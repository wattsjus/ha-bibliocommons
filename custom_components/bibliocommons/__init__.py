"""BiblioCommons integration for Home Assistant."""
from __future__ import annotations

import logging
import re
from collections import defaultdict
from datetime import date, datetime, time, timedelta
from urllib.parse import urljoin, urlparse

import requests
import voluptuous as vol
from bs4 import BeautifulSoup

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import async_track_time_change
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
import homeassistant.util.dt as dt_util

from .const import (
    DOMAIN,
    CONF_USERNAME,
    CONF_PASSWORD,
    CONF_LIBRARY_URL,
    CONF_LIBRARY_ICON_URL,
    CONF_LIBRARY_CARD_NUMBER,
    CONF_ASSIGNEES,
    CONF_ALERT_PEOPLE,
    CONF_BOOK_ASSIGNMENTS,
    CONF_BOOK_HISTORY,
    CONF_BOOK_REPORTS,
    CONF_LAST_BOOKS,
    CONF_READING_LEVEL_CACHE,
    CONF_PENDING_MISSING_BOOKS,
    CONF_PENDING_MISSING_AT,
    CONF_DAILY_REMINDERS,
    CONF_REMINDER_TIME,
    CONF_DUE_SOON_DAYS,
    CONF_BOOK_KEY,
    CONF_ASSIGNEE,
    CONF_ENTRY_ID,
    CONF_PERSON_ENTITY_ID,
    CONF_REPORT_CONFLICT,
    CONF_REPORT_CHARACTER_CHANGE,
    CONF_REPORT_THEME,
    CONF_REPORT_RECOMMENDATION,
    CONF_REPORT_HOURS,
    CONF_REPORT_STATUS,
    CONF_REPORT_SCREEN_MINUTES,
    CONF_REPORT_REVIEW_NOTE,
    SERVICE_ASSIGN_BOOK,
    SERVICE_SEND_DUE_REMINDERS,
    SERVICE_SYNC_BOOKS,
    SERVICE_SUBMIT_BOOK_REPORT,
    SERVICE_REVIEW_BOOK_REPORT,
    DEFAULT_DAILY_REMINDERS,
    DEFAULT_REMINDER_TIME,
    DEFAULT_DUE_SOON_DAYS,
SCAN_INTERVAL,
)

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.SENSOR]
EVENT_BOOK_REPORT_APPROVED = "bibliocommons_book_report_approved"
READING_LEVEL_LOOKUP_LIMIT = 3
READING_LEVEL_RETRY_DAYS = 7
READING_LEVEL_TIMEOUT = 8

ASSIGN_BOOK_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_BOOK_KEY): str,
        vol.Optional(CONF_ASSIGNEE, default=[]): vol.Any(str, [str]),
        vol.Optional(CONF_ENTRY_ID): str,
    }
)

SUBMIT_BOOK_REPORT_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_BOOK_KEY): str,
        vol.Required(CONF_PERSON_ENTITY_ID): str,
        vol.Required(CONF_REPORT_CONFLICT): str,
        vol.Required(CONF_REPORT_CHARACTER_CHANGE): str,
        vol.Required(CONF_REPORT_THEME): str,
        vol.Required(CONF_REPORT_RECOMMENDATION): str,
        vol.Required(CONF_REPORT_HOURS): vol.Coerce(float),
        vol.Optional(CONF_ENTRY_ID): str,
    }
)

REVIEW_BOOK_REPORT_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_BOOK_KEY): str,
        vol.Required(CONF_PERSON_ENTITY_ID): str,
        vol.Required(CONF_REPORT_STATUS): vol.In(["approved", "redo"]),
        vol.Optional(CONF_REPORT_SCREEN_MINUTES, default=0): vol.Coerce(int),
        vol.Optional(CONF_REPORT_REVIEW_NOTE, default=""): str,
        vol.Optional(CONF_ENTRY_ID): str,
    }
)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
}

MAX_BOOK_HISTORY = 500


def _clean_text(element) -> str:
    """Return normalized text from a BeautifulSoup element."""
    return " ".join(element.get_text(" ", strip=True).split()) if element else ""


def _remove_text(value: str, *parts: str) -> str:
    """Remove known child text from a larger scraped string."""
    cleaned = value
    for part in parts:
        if part:
            cleaned = cleaned.replace(part, " ")
    return " ".join(cleaned.split())


def _normalize_match_text(value: str) -> str:
    """Normalize scraped book text for resilient assignment matching."""
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).strip()


def _first_text(item, *selectors: str) -> str:
    """Return normalized text from the first matching selector."""
    for selector in selectors:
        if text := _clean_text(item.select_one(selector)):
            return text
    return ""


def _cover_map_from_html(html: str) -> dict[str, str]:
    """Extract BiblioCommons cover image URLs keyed by metadata id."""
    covers: dict[str, str] = {}
    matches = list(re.finditer(r'"metadataId":"(?P<record_id>[^"]+)"', html))
    for index, match in enumerate(matches):
        record_id = match.group("record_id")
        section_end = matches[index + 1].start() if index + 1 < len(matches) else len(html)
        section = html[match.end():section_end]
        cover_url = ""
        for field in ("local_url", "large", "medium", "small"):
            field_match = re.search(rf'"{field}":(?:"(?P<url>[^"]+)"|null)', section)
            if field_match and field_match.group("url"):
                cover_url = field_match.group("url").replace("\\/", "/")
                break
        if cover_url:
            covers[record_id] = cover_url
    return covers


def _library_icon_from_soup(soup: BeautifulSoup, base_url: str) -> str:
    """Return the best favicon advertised by a page."""
    for rel in (
        "shortcut icon",
        "icon",
        "apple-touch-icon",
        "apple-touch-icon-precomposed",
    ):
        if link := soup.find("link", rel=lambda value: value and rel in value):
            if icon_url := link.get("href", "").strip():
                return urljoin(base_url, icon_url)

    for attrs in (
        {"property": "og:image"},
        {"name": "twitter:image"},
    ):
        if meta := soup.find("meta", attrs):
            if icon_url := meta.get("content", "").strip():
                return urljoin(base_url, icon_url)

    for img in soup.find_all("img"):
        text = " ".join(
            str(img.get(attr, ""))
            for attr in ("alt", "class", "id", "src")
        ).lower()
        if "logo" in text or "library" in text:
            if icon_url := img.get("src", "").strip():
                return urljoin(base_url, icon_url)

    return ""


def _library_card_number_from_html(html: str) -> str:
    """Extract the logged-in account barcode/card number from account HTML."""
    soup = BeautifulSoup(html, "html.parser")
    barcode_el = soup.select_one("[data-js='field-barcode'][data-text]")
    if barcode_el:
        return str(barcode_el.get("data-text", "")).strip()
    return ""


def bibliocommons_library_card_number(
    library_url: str,
    session: requests.Session,
) -> str:
    """Return the logged-in user's library card barcode number when exposed."""
    resp = session.get(f"{library_url.rstrip('/')}/account", timeout=12)
    resp.raise_for_status()
    return _library_card_number_from_html(resp.text)


def _bibliocommons_public_site_url(html: str = "", library_url: str = "") -> str:
    """Infer a library's public web site from BiblioCommons page metadata."""
    for pattern in (
        r'"bc\.domain":"(?P<domain>[^"]+)"',
        r'"domain":"(?P<domain>[^"]+)"',
    ):
        if match := re.search(pattern, html):
            domain = match.group("domain").strip()
            if "." in domain:
                return f"https://{domain}"
            if domain:
                return f"https://www.{domain}.org"
    parsed = urlparse(library_url)
    host = parsed.netloc.lower()
    if host.endswith(".bibliocommons.com"):
        subdomain = host.split(".", 1)[0]
        if subdomain and subdomain not in {"www", "catalog"}:
            return f"https://www.{subdomain}.org"
    return ""


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up BiblioCommons from a config entry."""
    _async_register_services(hass)
    _async_clean_entry_options(hass, entry)

    coordinator = BiblioCommonsCoordinator(hass, entry)
    await coordinator.async_config_entry_first_refresh()
    site_metadata = await hass.async_add_executor_job(
        bibliocommons_site_metadata,
        entry.data.get(CONF_LIBRARY_URL, "https://your-library.bibliocommons.com"),
        coordinator._session,
    )
    site_name = site_metadata.get("name", "")
    icon_url = site_metadata.get("icon_url", "")
    updated_data = dict(entry.data)
    if icon_url and updated_data.get(CONF_LIBRARY_ICON_URL) != icon_url:
        updated_data[CONF_LIBRARY_ICON_URL] = icon_url
    if site_name and entry.title != site_name:
        hass.config_entries.async_update_entry(entry, title=site_name, data=updated_data)
    elif updated_data != entry.data:
        hass.config_entries.async_update_entry(entry, data=updated_data)
    _async_clean_entry_options(hass, entry)

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = coordinator
    entry.async_on_unload(entry.add_update_listener(async_update_options))
    _async_setup_reminders(hass, entry, coordinator)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


def _async_register_services(hass: HomeAssistant) -> None:
    """Register BiblioCommons services once."""
    if (
        hass.services.has_service(DOMAIN, SERVICE_ASSIGN_BOOK)
        and hass.services.has_service(DOMAIN, SERVICE_SEND_DUE_REMINDERS)
        and hass.services.has_service(DOMAIN, SERVICE_SYNC_BOOKS)
        and hass.services.has_service(DOMAIN, SERVICE_SUBMIT_BOOK_REPORT)
        and hass.services.has_service(DOMAIN, SERVICE_REVIEW_BOOK_REPORT)
    ):
        return

    async def async_assign_book(call) -> None:
        """Assign a checked-out book to a Home Assistant person."""
        book_key = call.data[CONF_BOOK_KEY]
        assignees = _assignment_person_ids(call.data.get(CONF_ASSIGNEE, []))
        entry_id = call.data.get(CONF_ENTRY_ID)
        entries = hass.config_entries.async_entries(DOMAIN)
        if entry_id:
            entries = [entry for entry in entries if entry.entry_id == entry_id]

        for entry in entries:
            options = dict(entry.options)
            assignments = _clean_book_assignments(options)

            if assignees:
                assignments[book_key] = assignees
            else:
                assignments.pop(book_key, None)

            options.pop(CONF_ASSIGNEES, None)
            options[CONF_ALERT_PEOPLE] = _alert_recipient_entity_ids(hass, options)
            options[CONF_BOOK_ASSIGNMENTS] = assignments
            hass.config_entries.async_update_entry(entry, options=options)

    async def async_send_due_reminders(call) -> None:
        """Send due-date reminders immediately."""
        entry_id = call.data.get(CONF_ENTRY_ID)
        entries = hass.config_entries.async_entries(DOMAIN)
        if entry_id:
            entries = [entry for entry in entries if entry.entry_id == entry_id]
        for entry in entries:
            coordinator = hass.data.get(DOMAIN, {}).get(entry.entry_id)
            if coordinator:
                await _async_send_due_reminders(hass, entry, coordinator)

    async def async_sync_books(call) -> None:
        """Refresh checked-out books immediately."""
        entry_id = call.data.get(CONF_ENTRY_ID)
        entries = hass.config_entries.async_entries(DOMAIN)
        if entry_id:
            entries = [entry for entry in entries if entry.entry_id == entry_id]
        for entry in entries:
            coordinator = hass.data.get(DOMAIN, {}).get(entry.entry_id)
            if coordinator:
                await coordinator.async_request_refresh()

    async def async_submit_book_report(call) -> None:
        """Save or update a person's book report for a book."""
        book_key = call.data[CONF_BOOK_KEY]
        person = call.data[CONF_PERSON_ENTITY_ID].strip()
        if not person.startswith("person."):
            _LOGGER.warning("Ignoring book report for non-person value: %s", person)
            return
        entry_id = call.data.get(CONF_ENTRY_ID)
        entries = hass.config_entries.async_entries(DOMAIN)
        if entry_id:
            entries = [entry for entry in entries if entry.entry_id == entry_id]

        for entry in entries:
            options = dict(entry.options)
            person_name = _person_name(hass, person)
            reports = [
                report
                for report in _clean_book_reports(
                    hass,
                    options.get(CONF_BOOK_REPORTS, []),
                )
                if not (
                    report.get("book_key") == book_key
                    and report.get("person_entity_id") == person
                )
            ]
            reports.append(
                {
                    "book_key": book_key,
                    "person_entity_id": person,
                    "person_name": person_name,
                    "main_conflict": call.data[CONF_REPORT_CONFLICT].strip(),
                    "character_change": call.data[CONF_REPORT_CHARACTER_CHANGE].strip(),
                    "theme": call.data[CONF_REPORT_THEME].strip(),
                    "recommendation": call.data[CONF_REPORT_RECOMMENDATION].strip(),
                    "hours_reading": max(0, float(call.data.get(CONF_REPORT_HOURS, 0))),
                    "report_status": "submitted",
                    "screen_time_minutes": 0,
                    "review_note": "",
                    "updated_at": dt_util.utcnow().replace(microsecond=0).isoformat(),
                }
            )
            options[CONF_BOOK_REPORTS] = reports
            hass.config_entries.async_update_entry(entry, options=options)
            await _async_notify_book_report_submitted(
                hass,
                entry,
                book_key,
                person_name,
            )

    async def async_review_book_report(call) -> None:
        """Approve or return a book report for redo."""
        book_key = call.data[CONF_BOOK_KEY]
        person = call.data[CONF_PERSON_ENTITY_ID].strip()
        status = call.data[CONF_REPORT_STATUS]
        screen_minutes = max(0, int(call.data.get(CONF_REPORT_SCREEN_MINUTES, 0)))
        review_note = call.data.get(CONF_REPORT_REVIEW_NOTE, "").strip()
        entry_id = call.data.get(CONF_ENTRY_ID)
        entries = hass.config_entries.async_entries(DOMAIN)
        if entry_id:
            entries = [entry for entry in entries if entry.entry_id == entry_id]

        for entry in entries:
            options = dict(entry.options)
            reports = _clean_book_reports(hass, options.get(CONF_BOOK_REPORTS, []))
            updated_reports = []
            changed = False
            for report in reports:
                if report.get("book_key") == book_key and report.get("person_entity_id") == person:
                    report = dict(report)
                    report["report_status"] = status
                    report["screen_time_minutes"] = screen_minutes if status == "approved" else 0
                    report["review_note"] = review_note
                    report["reviewed_at"] = dt_util.utcnow().replace(microsecond=0).isoformat()
                    changed = True
                updated_reports.append(report)
            if not changed:
                continue
            options[CONF_BOOK_REPORTS] = updated_reports
            hass.config_entries.async_update_entry(entry, options=options)
            await _async_notify_book_report_review(
                hass,
                person,
                _title_from_book_key(book_key),
                status,
                screen_minutes,
                review_note,
            )
            if status == "approved":
                hass.bus.async_fire(
                    EVENT_BOOK_REPORT_APPROVED,
                    {
                        "entry_id": entry.entry_id,
                        "library_name": entry.title,
                        "book_key": book_key,
                        "book_title": _title_from_book_key(book_key),
                        "person_entity_id": person,
                        "person_name": _person_name(hass, person),
                        "screen_time_minutes": screen_minutes,
                        "review_note": review_note,
                    },
                )

    if not hass.services.has_service(DOMAIN, SERVICE_ASSIGN_BOOK):
        hass.services.async_register(
            DOMAIN,
            SERVICE_ASSIGN_BOOK,
            async_assign_book,
            schema=ASSIGN_BOOK_SCHEMA,
        )

    if not hass.services.has_service(DOMAIN, SERVICE_SEND_DUE_REMINDERS):
        hass.services.async_register(
            DOMAIN,
            SERVICE_SEND_DUE_REMINDERS,
            async_send_due_reminders,
            schema=vol.Schema({vol.Optional(CONF_ENTRY_ID): str}),
        )

    if not hass.services.has_service(DOMAIN, SERVICE_SYNC_BOOKS):
        hass.services.async_register(
            DOMAIN,
            SERVICE_SYNC_BOOKS,
            async_sync_books,
            schema=vol.Schema({vol.Optional(CONF_ENTRY_ID): str}),
        )

    if not hass.services.has_service(DOMAIN, SERVICE_SUBMIT_BOOK_REPORT):
        hass.services.async_register(
            DOMAIN,
            SERVICE_SUBMIT_BOOK_REPORT,
            async_submit_book_report,
            schema=SUBMIT_BOOK_REPORT_SCHEMA,
        )

    if not hass.services.has_service(DOMAIN, SERVICE_REVIEW_BOOK_REPORT):
        hass.services.async_register(
            DOMAIN,
            SERVICE_REVIEW_BOOK_REPORT,
            async_review_book_report,
            schema=REVIEW_BOOK_REPORT_SCHEMA,
        )


def _person_entity_ids(hass: HomeAssistant) -> list[str]:
    """Return enabled Home Assistant person entity ids."""
    entity_ids = set()
    registry = er.async_get(hass)
    for entry in registry.entities.values():
        if entry.domain == "person" and not getattr(entry, "disabled_by", None):
            entity_ids.add(entry.entity_id)
    for state in hass.states.async_all("person"):
        entity_ids.add(state.entity_id)
    return sorted(entity_ids)


def _person_name(hass: HomeAssistant, person_entity_id: str) -> str:
    """Resolve a person entity id to a friendly name."""
    state = hass.states.get(person_entity_id)
    if state:
        return state.attributes.get("friendly_name", state.name)
    return person_entity_id


def _assignment_person_ids(value) -> list[str]:
    """Normalize one assignment value to a list of person entity ids."""
    values = value if isinstance(value, list) else [value]
    people = []
    seen = set()
    for item in values:
        person = str(item or "").strip()
        if person.startswith("person.") and person not in seen:
            seen.add(person)
            people.append(person)
    return people


def _clean_book_assignments(options: dict) -> dict[str, list[str]]:
    """Normalize stored book assignments to person-id lists."""
    raw_assignments = options.get(CONF_BOOK_ASSIGNMENTS, {})
    if not isinstance(raw_assignments, dict):
        return {}
    assignments = {}
    for key, value in raw_assignments.items():
        people = _assignment_person_ids(value)
        if people:
            assignments[str(key)] = people
    return assignments


def _alert_recipient_entity_ids(
    hass: HomeAssistant,
    options: dict,
) -> list[str]:
    """Return configured people who should always get due-date alerts."""
    person_ids = set(_person_entity_ids(hass))
    configured = options.get(CONF_ALERT_PEOPLE, options.get(CONF_ASSIGNEES))
    if isinstance(configured, list):
        return [
            person_id
            for person_id in configured
            if isinstance(person_id, str) and person_id in person_ids
        ]
    return sorted(person_ids)


def _clean_book_history(hass: HomeAssistant, history: list) -> list[dict[str, str]]:
    """Normalize and cap returned-book history entries."""
    if not isinstance(history, list):
        return []

    by_identity: dict[tuple[str, str, str], dict[str, str]] = {}
    for item in history:
        if not isinstance(item, dict):
            continue
        assignee = item.get("assignee_entity_id", item.get("assignee", ""))
        if assignee and not str(assignee).startswith("person."):
            continue
        entry = {
            "book_key": str(item.get("book_key", "")),
            "title": str(item.get("title", "") or "Unknown Title"),
            "author": str(item.get("author", "")),
            "assignee_entity_id": str(assignee),
            "assignee_name": str(item.get("assignee_name", ""))
            or _person_name(hass, str(assignee)),
            "returned_at": str(item.get("returned_at", "")),
            "library_name": str(item.get("library_name", "")),
            "library_url": str(item.get("library_url", "")),
        }
        identity = (
            entry["book_key"],
            entry["assignee_entity_id"],
            entry["library_name"],
        )
        if not entry["book_key"]:
            continue
        current = by_identity.get(identity)
        if not current or entry.get("returned_at", "") > current.get("returned_at", ""):
            by_identity[identity] = entry

    cleaned = list(by_identity.values())
    cleaned.sort(key=lambda item: item.get("returned_at", ""), reverse=True)
    return cleaned[:MAX_BOOK_HISTORY]


def _clean_book_reports(hass: HomeAssistant, reports: list) -> list[dict[str, str | int | float]]:
    """Normalize stored book report entries."""
    if not isinstance(reports, list):
        return []

    cleaned = []
    seen = set()
    for item in reports:
        if not isinstance(item, dict):
            continue
        book_key = str(item.get("book_key", ""))
        person = str(item.get("person_entity_id", ""))
        if not book_key or not person.startswith("person."):
            continue
        identity = (book_key, person)
        if identity in seen:
            continue
        seen.add(identity)
        cleaned.append(
            {
                "book_key": book_key,
                "person_entity_id": person,
                "person_name": str(item.get("person_name", ""))
                or _person_name(hass, person),
                "main_conflict": str(item.get("main_conflict", "")),
                "character_change": str(item.get("character_change", "")),
                "theme": str(item.get("theme", "")),
                "recommendation": str(item.get("recommendation", "")),
                "hours_reading": float(item.get("hours_reading", 0) or 0),
                "report_status": str(item.get("report_status", "submitted") or "submitted"),
                "screen_time_minutes": int(item.get("screen_time_minutes", 0) or 0),
                "review_note": str(item.get("review_note", "")),
                "updated_at": str(item.get("updated_at", "")),
                "reviewed_at": str(item.get("reviewed_at", "")),
            }
        )
    return cleaned


def _clean_cached_books(books: list) -> list[dict[str, str]]:
    """Normalize cached checkout book data stored in config entry options."""
    if not isinstance(books, list):
        return []

    cleaned = []
    seen = set()
    fields = (
        "title",
        "author",
        "due_date",
        "description",
        "status",
        "renewals",
        "format",
        "record_id",
        "book_url",
        "cover_image",
        "lexile_level",
        "reading_level",
        "reading_level_source",
    )
    for item in books:
        if not isinstance(item, dict):
            continue
        book = {field: str(item.get(field, "")) for field in fields}
        if not book["title"]:
            continue
        key = _book_key(book)
        if key in seen:
            continue
        seen.add(key)
        cleaned.append(book)
    return cleaned


def _clean_reading_level_cache(cache: dict) -> dict[str, dict[str, str]]:
    """Normalize cached reading-level lookup results."""
    if not isinstance(cache, dict):
        return {}

    cleaned = {}
    for key, item in cache.items():
        if not isinstance(key, str) or not key or not isinstance(item, dict):
            continue
        cleaned[key] = {
            "lexile_level": str(item.get("lexile_level", "")),
            "reading_level": str(item.get("reading_level", "")),
            "reading_level_source": str(item.get("reading_level_source", "")),
            "checked_at": str(item.get("checked_at", "")),
        }
    return cleaned


def _reading_level_cache_key(book: dict[str, str]) -> str:
    """Return a stable cache key for title-detail metadata."""
    return str(book.get("record_id") or book.get("book_url") or _book_key(book))


def _reading_level_cache_is_stale(item: dict[str, str]) -> bool:
    """Return true if a cached miss is old enough to retry."""
    checked_at = item.get("checked_at", "")
    if not checked_at:
        return True
    try:
        checked = datetime.fromisoformat(checked_at)
    except ValueError:
        return True
    return dt_util.utcnow() - checked >= timedelta(days=READING_LEVEL_RETRY_DAYS)


def _apply_reading_level(book: dict[str, str], data: dict[str, str]) -> None:
    """Copy cached reading-level metadata onto a book."""
    for field in ("lexile_level", "reading_level", "reading_level_source"):
        value = str(data.get(field, ""))
        if value:
            book[field] = value


def _extract_reading_level_data(html: str) -> dict[str, str]:
    """Extract Lexile or broader reading-level data from a title detail page."""
    soup = BeautifulSoup(html, "html.parser")
    text = _clean_text(soup)
    data: dict[str, str] = {}

    lexile_patterns = (
        r"\bLexile(?:\s+Measure|\s+Level)?\s*[:\-]?\s*([A-Z]{0,3}\s*\d{2,4}L|BR\s*\d{0,4}L?|NP)\b",
        r"\b([A-Z]{0,3}\s*\d{2,4}L|BR\s*\d{0,4}L?)\s+Lexile\b",
    )
    for pattern in lexile_patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            data["lexile_level"] = re.sub(r"\s+", "", match.group(1).upper())
            data["reading_level_source"] = "BiblioCommons title detail"
            break

    ar_match = re.search(
        r"\bAccelerated Reader\s*[:\-]?\s*([0-9]+(?:\.[0-9]+)?)\b",
        text,
        flags=re.IGNORECASE,
    )
    if ar_match:
        data["reading_level"] = f"AR {ar_match.group(1)}"
        data.setdefault("reading_level_source", "BiblioCommons title detail")

    reading_match = re.search(
        r"\bReading Level\s*[:\-]?\s*([^|,\n\r]{1,48})",
        text,
        flags=re.IGNORECASE,
    )
    if reading_match:
        reading_level = _clean_text(reading_match.group(1))
        if reading_level and "similar reading level" not in reading_level.lower():
            data.setdefault("reading_level", reading_level)
            data.setdefault("reading_level_source", "BiblioCommons title detail")

    return data


def _lookup_book_reading_level(
    session: requests.Session,
    book: dict[str, str],
) -> dict[str, str]:
    """Fetch and parse reading-level metadata for one book."""
    book_url = str(book.get("book_url", ""))
    if not book_url:
        return {}
    resp = session.get(book_url, timeout=READING_LEVEL_TIMEOUT)
    resp.raise_for_status()
    return _extract_reading_level_data(resp.text)


def _enrich_books_with_reading_levels(
    books: list[dict[str, str]],
    session: requests.Session,
    cache: dict,
) -> tuple[list[dict[str, str]], dict[str, dict[str, str]]]:
    """Apply cached reading levels and slowly look up missing values."""
    cleaned_cache = _clean_reading_level_cache(cache)
    lookups_remaining = READING_LEVEL_LOOKUP_LIMIT

    for book in books:
        cache_key = _reading_level_cache_key(book)
        cached = cleaned_cache.get(cache_key)
        if cached:
            _apply_reading_level(book, cached)
            if not _reading_level_cache_is_stale(cached):
                continue
        if lookups_remaining <= 0 or not book.get("book_url"):
            continue

        try:
            data = _lookup_book_reading_level(session, book)
        except requests.RequestException as err:
            _LOGGER.debug(
                "Unable to fetch reading level for %s: %s",
                book.get("title", "Unknown Title"),
                err,
            )
            cleaned_cache[cache_key] = {
                "lexile_level": "",
                "reading_level": "",
                "reading_level_source": "",
                "checked_at": dt_util.utcnow().replace(microsecond=0).isoformat(),
            }
            lookups_remaining -= 1
            continue

        cache_item = dict(data)
        cache_item["checked_at"] = dt_util.utcnow().replace(microsecond=0).isoformat()
        cleaned_cache[cache_key] = cache_item
        _apply_reading_level(book, cache_item)
        lookups_remaining -= 1

    return books, cleaned_cache


def _book_key_set(books: list[dict[str, str]]) -> set[str]:
    """Return assignment/display keys for a list of books."""
    return {_book_key(book) for book in books}


def _history_entry_from_returned_book(
    hass: HomeAssistant,
    entry: ConfigEntry,
    book_key: str,
    assignee: str,
) -> dict[str, str]:
    """Create a reading-history entry for a returned assigned book."""
    return {
        "book_key": book_key,
        "title": _title_from_book_key(book_key),
        "author": _author_from_book_key(book_key),
        "assignee_entity_id": assignee,
        "assignee_name": _person_name(hass, assignee),
        "returned_at": dt_util.utcnow().replace(microsecond=0).isoformat(),
        "library_name": entry.title,
        "library_url": entry.data.get(CONF_LIBRARY_URL, ""),
    }


def _async_clean_entry_options(
    hass: HomeAssistant,
    entry: ConfigEntry,
) -> None:
    """Remove legacy string people and keep assignment data person-based."""
    options = dict(entry.options)
    alert_recipients = _alert_recipient_entity_ids(hass, options)
    assignments = _clean_book_assignments(options)
    history = _clean_book_history(hass, options.get(CONF_BOOK_HISTORY, []))
    reports = _clean_book_reports(hass, options.get(CONF_BOOK_REPORTS, []))
    last_books = _clean_cached_books(options.get(CONF_LAST_BOOKS, []))
    pending_missing = [
        key
        for key in options.get(CONF_PENDING_MISSING_BOOKS, [])
        if isinstance(key, str)
    ]
    pending_missing_at = options.get(CONF_PENDING_MISSING_AT)

    cleaned = dict(options)
    cleaned.pop("child_names", None)
    cleaned.pop(CONF_ASSIGNEES, None)
    cleaned[CONF_ALERT_PEOPLE] = alert_recipients
    cleaned[CONF_BOOK_ASSIGNMENTS] = assignments
    cleaned[CONF_BOOK_HISTORY] = history
    cleaned[CONF_BOOK_REPORTS] = reports
    cleaned.pop("book_report_minutes_migrated", None)
    if last_books:
        cleaned[CONF_LAST_BOOKS] = last_books
    if pending_missing:
        cleaned[CONF_PENDING_MISSING_BOOKS] = pending_missing
    if isinstance(pending_missing_at, str) and pending_missing_at:
        cleaned[CONF_PENDING_MISSING_AT] = pending_missing_at
    if cleaned != options:
        hass.config_entries.async_update_entry(entry, options=cleaned)


async def _async_prune_returned_assignments(
    hass: HomeAssistant,
    entry: ConfigEntry,
    books: list[dict[str, str]],
    notify: bool,
) -> None:
    """Remove assignments for returned books and optionally thank each person."""
    if not books:
        _LOGGER.warning(
            "Skipping assignment pruning for %s because the latest sync returned no books",
            entry.title,
        )
        return

    options = dict(entry.options)
    assignments = _clean_book_assignments(options)
    removed_by_person: dict[str, list[str]] = defaultdict(list)
    returned_history = []
    kept_assignments = {}
    by_key, unique_full, unique_title = _active_book_match_indexes(books)

    for book_key, assignees in assignments.items():
        active_key = _active_book_key_for_assignment(
            book_key,
            by_key,
            unique_full,
            unique_title,
        )
        if active_key:
            kept_assignments[active_key] = assignees
        else:
            for assignee in assignees:
                removed_by_person[assignee].append(book_key)
                returned_history.append(
                    _history_entry_from_returned_book(hass, entry, book_key, assignee)
                )

    cleaned = dict(options)
    cleaned.pop("child_names", None)
    cleaned.pop(CONF_ASSIGNEES, None)
    cleaned[CONF_ALERT_PEOPLE] = _alert_recipient_entity_ids(hass, options)
    cleaned[CONF_BOOK_ASSIGNMENTS] = kept_assignments
    if returned_history:
        cleaned[CONF_BOOK_HISTORY] = _clean_book_history(
            hass,
            list(options.get(CONF_BOOK_HISTORY, [])) + returned_history,
        )
    if cleaned != options:
        hass.config_entries.async_update_entry(entry, options=cleaned)

    if notify and removed_by_person:
        await _async_send_return_notifications(hass, removed_by_person)


def _book_merge_with_cached(
    current_books: list[dict[str, str]],
    cached_books: list[dict[str, str]],
    missing_keys: set[str],
) -> list[dict[str, str]]:
    """Return current books plus cached books that need another sync to confirm."""
    merged = list(current_books)
    current_keys = _book_key_set(current_books)
    for book in cached_books:
        key = _book_key(book)
        if key in missing_keys and key not in current_keys:
            merged.append(book)
    return merged


async def _async_update_cached_books_after_sync(
    hass: HomeAssistant,
    entry: ConfigEntry,
    current_books: list[dict[str, str]],
    notify: bool,
) -> list[dict[str, str]]:
    """Treat the latest successful checkout sync as authoritative."""
    options = dict(entry.options)
    cached_books = _clean_cached_books(options.get(CONF_LAST_BOOKS, []))
    current_books = _clean_cached_books(current_books)
    cached_keys = _book_key_set(cached_books)
    current_keys = _book_key_set(current_books)
    missing_keys = cached_keys - current_keys

    if cached_books and missing_keys:
        pending_keys = {
            key
            for key in options.get(CONF_PENDING_MISSING_BOOKS, [])
            if isinstance(key, str)
        }
        if pending_keys != missing_keys:
            merged_books = _book_merge_with_cached(
                current_books,
                cached_books,
                missing_keys,
            )
            updated = dict(options)
            updated[CONF_LAST_BOOKS] = _clean_cached_books(merged_books)
            updated[CONF_PENDING_MISSING_BOOKS] = sorted(missing_keys)
            updated[CONF_PENDING_MISSING_AT] = (
                dt_util.utcnow().replace(microsecond=0).isoformat()
            )
            if updated != options:
                hass.config_entries.async_update_entry(entry, options=updated)
            _LOGGER.warning(
                "Keeping %s cached books until the next sync confirms they are returned",
                len(missing_keys),
            )
            return merged_books

    updated = dict(options)
    updated[CONF_LAST_BOOKS] = current_books
    updated.pop(CONF_PENDING_MISSING_BOOKS, None)
    updated.pop(CONF_PENDING_MISSING_AT, None)
    if updated != options:
        hass.config_entries.async_update_entry(entry, options=updated)

    await _async_prune_returned_assignments(
        hass,
        entry,
        current_books,
        notify=notify or bool(cached_books),
    )
    return current_books


async def _async_update_reading_level_cache(
    hass: HomeAssistant,
    entry: ConfigEntry,
    cache: dict[str, dict[str, str]],
) -> None:
    """Store reading-level lookup cache in config entry options."""
    cleaned_cache = _clean_reading_level_cache(cache)
    if not cleaned_cache:
        return
    options = dict(entry.options)
    updated = dict(options)
    updated[CONF_READING_LEVEL_CACHE] = cleaned_cache
    if updated != options:
        hass.config_entries.async_update_entry(entry, options=updated)


async def _async_update_library_card_number(
    hass: HomeAssistant,
    entry: ConfigEntry,
    card_number: str,
) -> None:
    """Store the discovered library card barcode number."""
    if not card_number:
        return
    options = dict(entry.options)
    if options.get(CONF_LIBRARY_CARD_NUMBER) == card_number:
        return
    updated = dict(options)
    updated[CONF_LIBRARY_CARD_NUMBER] = card_number
    hass.config_entries.async_update_entry(entry, options=updated)


async def async_update_options(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Refresh entity attributes when options change."""
    coordinator = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    if coordinator:
        coordinator.async_set_updated_data(coordinator.data)
        _async_setup_reminders(hass, entry, coordinator)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    _async_clear_reminders(hass, entry)
    if unload_ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        hass.data[DOMAIN].pop(entry.entry_id)
    return unload_ok


def _reminder_unsub_key(entry: ConfigEntry) -> str:
    """Return hass.data key for the reminder unsubscribe callback."""
    return f"{entry.entry_id}_reminder_unsub"


def _async_clear_reminders(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Remove any scheduled due-date reminder for this entry."""
    unsub = hass.data.get(DOMAIN, {}).pop(_reminder_unsub_key(entry), None)
    if unsub:
        unsub()


def _parse_reminder_time(value: str) -> time:
    """Parse a Home Assistant time selector value."""
    if isinstance(value, time):
        return value
    for fmt in ("%H:%M:%S", "%H:%M"):
        try:
            return datetime.strptime(str(value), fmt).time()
        except ValueError:
            continue
    return datetime.strptime(DEFAULT_REMINDER_TIME, "%H:%M:%S").time()


def _async_setup_reminders(
    hass: HomeAssistant,
    entry: ConfigEntry,
    coordinator: "BiblioCommonsCoordinator",
) -> None:
    """Schedule daily reminders for due and overdue assigned books."""
    _async_clear_reminders(hass, entry)
    enabled = entry.options.get(CONF_DAILY_REMINDERS, DEFAULT_DAILY_REMINDERS)
    if not enabled:
        return

    reminder_time = _parse_reminder_time(
        entry.options.get(CONF_REMINDER_TIME, DEFAULT_REMINDER_TIME)
    )

    @callback
    def _run_reminders(now) -> None:
        hass.async_create_task(_async_send_due_reminders(hass, entry, coordinator))

    unsub = async_track_time_change(
        hass,
        _run_reminders,
        hour=reminder_time.hour,
        minute=reminder_time.minute,
        second=reminder_time.second,
    )
    hass.data.setdefault(DOMAIN, {})[_reminder_unsub_key(entry)] = unsub


def _book_key(book: dict[str, str]) -> str:
    """Create the display key used for book assignments."""
    title = book.get("title") or "Unknown Title"
    author = book.get("author") or ""
    return f"{title} - {author}" if author else title


def _title_from_book_key(book_key: str) -> str:
    """Return a readable title from an assignment key."""
    return book_key.rsplit(" - ", 1)[0]


def _author_from_book_key(book_key: str) -> str:
    """Return a readable author from an assignment key."""
    if " - " not in book_key:
        return ""
    return book_key.rsplit(" - ", 1)[1]


def _book_match_tokens_from_key(book_key: str) -> tuple[str, str]:
    """Return full and title-only match tokens for an assignment key."""
    return (
        _normalize_match_text(book_key),
        _normalize_match_text(_title_from_book_key(book_key)),
    )


def _active_book_match_indexes(
    books: list[dict[str, str]],
) -> tuple[dict[str, dict[str, str]], dict[str, str], dict[str, str]]:
    """Build exact and fuzzy indexes for active books."""
    by_key = {}
    full_candidates: dict[str, list[str]] = defaultdict(list)
    title_candidates: dict[str, list[str]] = defaultdict(list)

    for book in books:
        key = _book_key(book)
        by_key[key] = book
        full_token = _normalize_match_text(key)
        title_token = _normalize_match_text(book.get("title", ""))
        if full_token:
            full_candidates[full_token].append(key)
        if title_token:
            title_candidates[title_token].append(key)

    unique_full = {
        token: keys[0]
        for token, keys in full_candidates.items()
        if len(set(keys)) == 1
    }
    unique_title = {
        token: keys[0]
        for token, keys in title_candidates.items()
        if len(set(keys)) == 1
    }
    return by_key, unique_full, unique_title


def _active_book_key_for_assignment(
    assignment_key: str,
    by_key: dict[str, dict[str, str]],
    unique_full: dict[str, str],
    unique_title: dict[str, str],
) -> str:
    """Return the current active book key matching an old assignment key."""
    if assignment_key in by_key:
        return assignment_key
    full_token, title_token = _book_match_tokens_from_key(assignment_key)
    if full_token and full_token in unique_full:
        return unique_full[full_token]
    if title_token and title_token in unique_title:
        return unique_title[title_token]
    return ""


def _parse_due_date(value: str, today: date) -> date | None:
    """Parse BiblioCommons due date text into a date."""
    if not value:
        return None

    text = " ".join(str(value).replace(",", ", ").split())
    lowered = text.lower()
    if "today" in lowered:
        return today
    if "tomorrow" in lowered:
        return today + timedelta(days=1)
    if "yesterday" in lowered:
        return today - timedelta(days=1)

    text = re.sub(r"^(due|due on|renew by)\s+", "", text, flags=re.IGNORECASE)
    month_names = (
        "jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec|"
        "january|february|march|april|june|july|august|september|"
        "october|november|december"
    )
    month_match = re.search(
        rf"\b({month_names})\.?\s+\d{{1,2}}(?:,\s*\d{{4}})?\b",
        text,
        flags=re.IGNORECASE,
    )
    candidates = [month_match.group(0).replace(".", "")] if month_match else [text]

    numeric_match = re.search(r"\b\d{1,2}/\d{1,2}/(?:\d{2}|\d{4})\b", text)
    if numeric_match:
        candidates.insert(0, numeric_match.group(0))

    for candidate in candidates:
        for fmt in ("%B %d, %Y", "%b %d, %Y", "%m/%d/%Y", "%m/%d/%y"):
            try:
                return datetime.strptime(candidate, fmt).date()
            except ValueError:
                continue
        for fmt in ("%B %d", "%b %d"):
            try:
                parsed = datetime.strptime(candidate, fmt).date().replace(
                    year=today.year
                )
                if parsed < today - timedelta(days=180):
                    parsed = parsed.replace(year=today.year + 1)
                return parsed
            except ValueError:
                continue
    return None


def _notify_entity_for_person(hass: HomeAssistant, person_entity_id: str) -> str | None:
    """Find the most likely notify entity for a Home Assistant person."""
    person_state = hass.states.get(person_entity_id)
    if not person_state:
        return None

    trackers = []
    source = person_state.attributes.get("source")
    if isinstance(source, str):
        trackers.append(source)
    for tracker in person_state.attributes.get("device_trackers", []):
        if isinstance(tracker, str) and tracker not in trackers:
            trackers.append(tracker)

    for tracker in trackers:
        if not tracker.startswith("device_tracker."):
            continue
        notify_entity_id = f"notify.{tracker.split('.', 1)[1]}"
        if hass.states.get(notify_entity_id):
            return notify_entity_id

    slug = person_entity_id.split(".", 1)[1]
    notify_entity_id = f"notify.{slug}"
    if hass.states.get(notify_entity_id):
        return notify_entity_id

    registry = er.async_get(hass)
    for entry in registry.entities.values():
        if entry.domain == "notify" and not getattr(entry, "disabled_by", None):
            name = (entry.name or entry.original_name or "").lower().replace(" ", "_")
            if slug in (entry.entity_id.split(".", 1)[1], name):
                return entry.entity_id
    return None


async def _async_send_notification(
    hass: HomeAssistant,
    notify_entity_id: str,
    title: str,
    message: str,
) -> bool:
    """Send a notification to a notify entity or service."""
    service = notify_entity_id.split(".", 1)[1]
    if hass.services.has_service("notify", service):
        await hass.services.async_call(
            "notify",
            service,
            {"title": title, "message": message},
            blocking=False,
        )
        return True

    if hass.services.has_service("notify", "send_message"):
        await hass.services.async_call(
            "notify",
            "send_message",
            {"title": title, "message": message},
            target={"entity_id": notify_entity_id},
            blocking=False,
        )
        return True

    return False


async def _async_notify_book_report_review(
    hass: HomeAssistant,
    person_entity_id: str,
    book_title: str,
    status: str,
    screen_minutes: int,
    review_note: str,
) -> None:
    """Notify a person that their book report was reviewed."""
    notify_entity_id = _notify_entity_for_person(hass, person_entity_id)
    if not notify_entity_id:
        _LOGGER.warning("No notify entity found for %s", person_entity_id)
        return
    if status == "approved":
        message = f"Your book report for {book_title} was approved."
        if screen_minutes:
            message = f"{message} You earned {screen_minutes} minutes of screen time."
    else:
        message = f"Please update your book report for {book_title}."
    if review_note:
        message = f"{message}\n{review_note}"
    await _async_send_notification(
        hass,
        notify_entity_id,
        "Book report reviewed",
        message,
    )


async def _async_notify_book_report_submitted(
    hass: HomeAssistant,
    entry: ConfigEntry,
    book_key: str,
    person_name: str,
) -> None:
    """Notify configured reviewers that a book report is ready."""
    recipients = _alert_recipient_entity_ids(hass, entry.options)
    if not recipients:
        return

    book_title = _title_from_book_key(book_key)
    message = f"{person_name} submitted a book report for {book_title}."
    if entry.title:
        message = f"{message}\n{entry.title}"

    for person_entity_id in recipients:
        notify_entity_id = _notify_entity_for_person(hass, person_entity_id)
        if not notify_entity_id:
            _LOGGER.warning("No notify entity found for %s", person_entity_id)
            continue
        await _async_send_notification(
            hass,
            notify_entity_id,
            "Book report ready for review",
            message,
        )


def _format_due_message(items: list[tuple[dict[str, str], int]]) -> str:
    """Format a concise daily reminder body."""
    if len(items) > 1:
        return f"You have {len(items)} library books due soon or overdue."

    lines = []
    for book, days_until_due in items:
        title = book.get("title", "Unknown Title")
        due_date = book.get("due_date", "Unknown")
        if days_until_due < 0:
            state = "overdue"
        elif days_until_due == 0:
            state = "due today"
        elif days_until_due == 1:
            state = "due tomorrow"
        else:
            state = f"due in {days_until_due} days"
        lines.append(f"{title} is {state} ({due_date}).")
    return "\n".join(lines)


def _format_due_summary_message(items: list[tuple[dict[str, str], int, str]]) -> str:
    """Format a reminder body for always-alert recipients."""
    if len(items) > 1:
        return f"There are {len(items)} library books due soon or overdue."

    lines = []
    for book, days_until_due, assignee in items:
        title = book.get("title", "Unknown Title")
        due_date = book.get("due_date", "Unknown")
        if days_until_due < 0:
            state = "overdue"
        elif days_until_due == 0:
            state = "due today"
        elif days_until_due == 1:
            state = "due tomorrow"
        else:
            state = f"due in {days_until_due} days"
        lines.append(f"{title} is {state} ({due_date}).")
    return "\n".join(lines)


def _format_return_message(book_keys: list[str]) -> str:
    """Format the returned-book thank-you message."""
    titles = [_title_from_book_key(book_key) for book_key in book_keys]
    if len(titles) == 1:
        return f"Thank you for returning {titles[0]}."
    title_lines = "\n".join(f"- {title}" for title in titles)
    return f"Thank you for returning your library books:\n{title_lines}"


async def _async_send_return_notifications(
    hass: HomeAssistant,
    removed_by_person: dict[str, list[str]],
) -> None:
    """Thank people for books that disappeared from their assigned list."""
    for assignee, book_keys in removed_by_person.items():
        notify_entity_id = _notify_entity_for_person(hass, assignee)
        if not notify_entity_id:
            _LOGGER.warning("No notify entity found for %s", assignee)
            continue
        person_state = hass.states.get(assignee)
        name = person_state.attributes.get("friendly_name", assignee) if person_state else assignee
        sent = await _async_send_notification(
            hass,
            notify_entity_id,
            "Library books returned",
            _format_return_message(book_keys),
        )
        if sent:
            _LOGGER.info("Sent library return thank-you to %s", name)
        else:
            _LOGGER.warning("No notify service available for %s", notify_entity_id)


async def _async_send_due_reminders(
    hass: HomeAssistant,
    entry: ConfigEntry,
    coordinator: "BiblioCommonsCoordinator",
) -> None:
    """Notify assigned people about overdue and nearly due books."""
    await coordinator.async_request_refresh()
    books = coordinator.data or []
    assignments = _clean_book_assignments(entry.options)
    due_soon_days = int(entry.options.get(CONF_DUE_SOON_DAYS, DEFAULT_DUE_SOON_DAYS))
    today = dt_util.now().date()
    by_person: dict[str, list[tuple[dict[str, str], int]]] = defaultdict(list)
    alert_recipients = set(_alert_recipient_entity_ids(hass, entry.options))
    alert_items: list[tuple[dict[str, str], int, str]] = []

    for book in books:
        assignees = assignments.get(_book_key(book), [])
        due = _parse_due_date(book.get("due_date", ""), today)
        if not due:
            continue
        days_until_due = (due - today).days
        is_overdue = "overdue" in book.get("status", "").lower() or days_until_due < 0
        if is_overdue or 0 <= days_until_due <= due_soon_days:
            alert_items.append((book, days_until_due, ", ".join(assignees)))
            for assignee in assignees:
                if assignee not in alert_recipients:
                    by_person[assignee].append((book, days_until_due))

    for alert_recipient in alert_recipients:
        if alert_items:
            notify_entity_id = _notify_entity_for_person(hass, alert_recipient)
            if not notify_entity_id:
                _LOGGER.warning("No notify entity found for %s", alert_recipient)
                continue
            person_state = hass.states.get(alert_recipient)
            name = (
                person_state.attributes.get("friendly_name", alert_recipient)
                if person_state
                else alert_recipient
            )
            sent = await _async_send_notification(
                hass,
                notify_entity_id,
                "Library reminder",
                _format_due_summary_message(alert_items),
            )
            if sent:
                _LOGGER.info("Sent library due-date summary reminder to %s", name)
            else:
                _LOGGER.warning("No notify service available for %s", notify_entity_id)

    for assignee, items in by_person.items():
        notify_entity_id = _notify_entity_for_person(hass, assignee)
        if not notify_entity_id:
            _LOGGER.warning("No notify entity found for %s", assignee)
            continue
        person_state = hass.states.get(assignee)
        name = person_state.attributes.get("friendly_name", assignee) if person_state else assignee
        sent = await _async_send_notification(
            hass,
            notify_entity_id,
            "Library reminder",
            _format_due_message(items),
        )
        if sent:
            _LOGGER.info("Sent library due-date reminder to %s", name)
        else:
            _LOGGER.warning("No notify service available for %s", notify_entity_id)


def bibliocommons_login(library_url: str, username: str, password: str) -> requests.Session:
    """
    Log into a BiblioCommons library site and return an authenticated session.

    BiblioCommons uses a multi-step OAuth-style login:
      1. GET  /user/login  — grab CSRF token + hidden fields
      2. POST /user/login  — submit credentials
      3. Follow redirect back to the library site with session established
    """
    session = requests.Session()
    session.headers.update(HEADERS)

    base = library_url.rstrip("/")
    login_url = f"{base}/user/login"

    # Step 1 — load the login page to collect form tokens
    resp = session.get(login_url, timeout=20)
    resp.raise_for_status()

    soup = BeautifulSoup(resp.text, "html.parser")

    # BiblioCommons generates dynamic form ids. Pick the form with a password/PIN field.
    form = None
    for candidate in soup.find_all("form"):
        if candidate.find("input", {"type": "password"}):
            form = candidate
            break
    form = form or soup.find("form", {"id": "login_form"}) or soup.find("form")

    payload: dict[str, str] = {}
    if form:
        for hidden in form.find_all("input", {"type": "hidden"}):
            name = hidden.get("name")
            value = hidden.get("value", "")
            if name:
                payload[name] = value

        username_input = (
            form.find("input", {"name": "name"})
            or form.find("input", {"name": "user[username]"})
            or form.find("input", {"type": "text"})
        )
        password_input = (
            form.find("input", {"name": "user_pin"})
            or form.find("input", {"name": "user[password]"})
            or form.find("input", {"type": "password"})
        )
        submit_input = form.find("input", {"type": "submit"})
        action = form.get("action") or login_url
    else:
        username_input = None
        password_input = None
        submit_input = None
        action = login_url

    username_field = username_input.get("name") if username_input else "name"
    password_field = password_input.get("name") if password_input else "user_pin"
    payload[username_field] = username
    payload[password_field] = password

    if submit_input and submit_input.get("name"):
        payload[submit_input["name"]] = submit_input.get("value", "Log In")

    # Step 2 — POST credentials
    post_resp = session.post(
        urljoin(login_url, action),
        data=payload,
        headers={"Referer": login_url, "Content-Type": "application/x-www-form-urlencoded"},
        timeout=20,
        allow_redirects=True,
    )
    post_resp.raise_for_status()

    # Verify we're actually logged in (not back on the login page with the login form).
    post_soup = BeautifulSoup(post_resp.text, "html.parser")
    still_has_login_form = any(
        candidate.find("input", {"type": "password"})
        for candidate in post_soup.find_all("form")
    )
    if (
        "sign_in" in post_resp.url
        or ("login" in post_resp.url and still_has_login_form)
        or "invalid" in post_resp.text.lower() and still_has_login_form
    ):
        raise ValueError("Login failed — check username and password.")

    _LOGGER.debug("BiblioCommons login successful, session cookies: %s", list(session.cookies.keys()))
    return session


def bibliocommons_site_name(
    library_url: str,
    session: requests.Session | None = None,
) -> str:
    """Return a friendly library name from the site or URL."""
    return bibliocommons_site_metadata(library_url, session).get("name", "")


def bibliocommons_site_metadata(
    library_url: str,
    session: requests.Session | None = None,
) -> dict[str, str]:
    """Return friendly library metadata from the catalog and public site."""
    base = library_url.rstrip("/")
    http = session or requests.Session()
    http.headers.update(HEADERS)
    fallback_name = _site_name_from_url(base)
    fallback_icon = f"{base}/favicon.ico"

    try:
        resp = http.get(base, timeout=20)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        candidates = []
        for attrs in (
            {"property": "og:site_name"},
            {"name": "application-name"},
            {"name": "apple-mobile-web-app-title"},
        ):
            if meta := soup.find("meta", attrs):
                candidates.append(meta.get("content", ""))
        if soup.title:
            candidates.append(_clean_text(soup.title))
        icon_url = ""
        if public_site_url := _bibliocommons_public_site_url(resp.text, base):
            try:
                public_resp = http.get(public_site_url, timeout=20)
                public_resp.raise_for_status()
                icon_url = _library_icon_from_soup(
                    BeautifulSoup(public_resp.text, "html.parser"),
                    public_site_url,
                )
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug(
                    "Could not read library icon from %s: %s",
                    public_site_url,
                    err,
                )
        icon_url = icon_url or _library_icon_from_soup(soup, base) or fallback_icon
        for candidate in candidates:
            if site_name := _clean_site_name(candidate):
                return {"name": site_name, "icon_url": icon_url}
    except Exception as err:  # noqa: BLE001
        _LOGGER.debug("Could not read BiblioCommons site name from %s: %s", base, err)

    if public_site_url := _bibliocommons_public_site_url(library_url=base):
        try:
            public_resp = http.get(public_site_url, timeout=20)
            public_resp.raise_for_status()
            soup = BeautifulSoup(public_resp.text, "html.parser")
            icon_url = _library_icon_from_soup(soup, public_site_url) or f"{public_site_url}/favicon.ico"
            site_name = _clean_site_name(_clean_text(soup.title) if soup.title else "")
            return {"name": site_name or fallback_name, "icon_url": icon_url}
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Could not read public library metadata from %s: %s", public_site_url, err)

    return {"name": fallback_name, "icon_url": fallback_icon}


def _clean_site_name(value: str) -> str:
    """Clean a page title or metadata value into a config entry title."""
    text = " ".join(str(value or "").split())
    if not text:
        return ""
    for separator in ("|", " - ", " — "):
        if separator in text:
            parts = [part.strip() for part in text.split(separator)]
            for part in parts:
                if part and part.lower() not in {"bibliocommons", "log in", "login"}:
                    text = part
                    break
    text = re.sub(r"\bBibliocommons\b", "", text, flags=re.IGNORECASE)
    text = " ".join(text.strip(" -|").split())
    return text if text and text.lower() not in {"log in", "login"} else ""


def _site_name_from_url(library_url: str) -> str:
    """Create a friendly library name from a BiblioCommons URL."""
    host = urlparse(library_url).hostname or library_url
    label = host.split(".", 1)[0].replace("-", " ").replace("_", " ")
    if label.lower() == "your-library":
        return "BiblioCommons"
    if label.lower().endswith("library") and len(label) > len("library"):
        stem = label[:-len("library")].strip()
        if stem:
            label = f"{stem} library"
    return label.title()


class BiblioCommonsCoordinator(DataUpdateCoordinator):
    """Coordinator to fetch checked out books from BiblioCommons."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Initialize the coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(minutes=SCAN_INTERVAL),
        )
        self.entry = entry
        self._session: requests.Session | None = None
        self._return_notifications_ready = False
        self.library_card_number = str(
            entry.options.get(CONF_LIBRARY_CARD_NUMBER, "")
            or entry.data.get(CONF_LIBRARY_CARD_NUMBER, "")
        )
        self._reading_level_cache: dict[str, dict[str, str]] = {}

    async def _async_update_data(self):
        """Fetch data from BiblioCommons, re-logging in if the session expired."""
        try:
            books = await self.hass.async_add_executor_job(self._fetch_checkouts)
            books = await _async_update_cached_books_after_sync(
                self.hass,
                self.entry,
                books,
                notify=self._return_notifications_ready,
            )
            await _async_update_reading_level_cache(
                self.hass,
                self.entry,
                self._reading_level_cache,
            )
            await _async_update_library_card_number(
                self.hass,
                self.entry,
                self.library_card_number,
            )
            self._return_notifications_ready = True
            return books
        except Exception as err:
            cached_books = _clean_cached_books(self.entry.options.get(CONF_LAST_BOOKS, []))
            if cached_books:
                _LOGGER.warning(
                    "Error fetching library data; keeping %s cached books: %s",
                    len(cached_books),
                    err,
                )
                return cached_books
            raise UpdateFailed(f"Error fetching library data: {err}") from err

    def _fetch_checkouts(self) -> list[dict]:
        """Synchronously log in (if needed) and scrape the checked out page."""
        library_url = self.entry.data.get(CONF_LIBRARY_URL, "https://your-library.bibliocommons.com")
        username = self.entry.data[CONF_USERNAME]
        password = self.entry.data[CONF_PASSWORD]
        checkedout_url = f"{library_url.rstrip('/')}/v2/checkedout"

        # Use cached session or create a new one
        if self._session is None:
            _LOGGER.debug("No active session — logging into BiblioCommons")
            self._session = bibliocommons_login(library_url, username, password)

        resp = self._session.get(checkedout_url, timeout=30)

        # If we got redirected to login, our session expired — re-authenticate once
        if "sign_in" in resp.url or "login" in resp.url or resp.status_code == 401:
            _LOGGER.info("Session expired — re-authenticating with BiblioCommons")
            self._session = bibliocommons_login(library_url, username, password)
            resp = self._session.get(checkedout_url, timeout=30)

        resp.raise_for_status()
        books = _parse_checkouts(resp.text, library_url)
        cached_books = _clean_cached_books(self.entry.options.get(CONF_LAST_BOOKS, []))
        if not books and cached_books and not _checkout_page_confirms_no_physical_books(resp.text):
            raise ValueError(
                "Checkout page did not include checkout items or a no-checkouts message; "
                "keeping the previous book list until the next successful sync"
            )
        if not self.library_card_number:
            try:
                self.library_card_number = bibliocommons_library_card_number(
                    library_url,
                    self._session,
                )
            except requests.RequestException as err:
                _LOGGER.debug("Unable to fetch library card barcode number: %s", err)
        books, self._reading_level_cache = _enrich_books_with_reading_levels(
            books,
            self._session,
            self.entry.options.get(CONF_READING_LEVEL_CACHE, {}),
        )
        return books


def _parse_checkouts(html: str, library_url: str = "") -> list[dict]:
    """Parse checked out books from BiblioCommons HTML."""
    soup = BeautifulSoup(html, "html.parser")
    books = []
    cover_map = _cover_map_from_html(html)

    items = _checkout_item_candidates(soup)

    for item in items:
        try:
            book = _extract_book_data(item, library_url, cover_map)
            if book and not _is_digital_checkout(book):
                books.append(book)
        except Exception:  # noqa: BLE001
            continue

    return books


def _checkout_item_candidates(soup: BeautifulSoup) -> list:
    """Return possible checkout item containers from a BiblioCommons account page."""
    return (
        soup.select(".cp-bib-list-item.cp-checked-out-item")
        or soup.select(".cp-checked-out-item")
        or soup.select(".cp-bib-list-item")
        or soup.select("li.item-list-bib")
        or soup.select(".bib-item")
        or [
            item
            for item in soup.select("[data-key]")
            if item.select_one("[data-key='bib-title'], .title-content, .checkout-status")
        ]
    )


def _checkout_page_confirms_no_physical_books(html: str) -> bool:
    """Return true when an empty parsed result looks authoritative."""
    soup = BeautifulSoup(html, "html.parser")
    if _checkout_item_candidates(soup):
        return True

    text = " ".join(soup.get_text(" ", strip=True).lower().split())
    empty_markers = (
        "no checked out",
        "no items checked out",
        "no titles checked out",
        "nothing checked out",
        "you do not have any checked out",
        "you have no checked out",
        "you have no titles checked out",
    )
    return any(marker in text for marker in empty_markers)


def _is_digital_checkout(book: dict) -> bool:
    """Return true for ebook and other digital checkout formats."""
    searchable = " ".join(
        str(book.get(field, ""))
        for field in ("format", "title", "description", "book_url")
    ).lower()
    digital_markers = (
        "ebook",
        "e-book",
        "eaudiobook",
        "e-audiobook",
        "e audiobook",
        "downloadable",
        "digital",
        "libby",
        "overdrive",
        "hoopla",
        "cloudlibrary",
        "axis 360",
    )
    return any(marker in searchable for marker in digital_markers)


def _extract_book_data(
    item,
    library_url: str = "",
    cover_map: dict[str, str] | None = None,
) -> dict | None:
    """Extract book data from a single list item."""
    title = _first_text(item, ".title-content", "[data-key='bib-title'] .title-content")
    subtitle = _first_text(item, ".cp-subtitle")
    if title and subtitle:
        title = f"{title}: {subtitle}"

    title_el = (
        item.select_one("[data-key='bib-title']")
        or item.select_one(".cp-title a")
        or item.select_one(".cp-bib-brief-info-title a")
        or item.select_one(".title a")
        or item.select_one("[data-title]")
        or item.select_one("a.title")
        or item.select_one("h2 a")
        or item.select_one("h3 a")
    )
    if not title and not title_el:
        return None
    title = title or _clean_text(title_el)
    title = _remove_text(title, _first_text(item, ".cp-screen-reader-message"))
    if not title:
        return None
    book_url = ""
    record_id = ""
    if title_el and title_el.get("href"):
        book_url = urljoin(library_url or "https://your-library.bibliocommons.com", title_el["href"])
        record_match = re.search(r"/record/([^/?#]+)", title_el["href"])
        if record_match:
            record_id = record_match.group(1)

    author_el = (
        item.select_one("a.author-link")
        or item.select_one(".cp-author-link")
        or item.select_one(".cp-bib-brief-info-author")
        or item.select_one(".author")
        or item.select_one("[data-author]")
    )
    author = _clean_text(author_el) if author_el else ""
    author = author.removeprefix("by ").strip() or "Unknown"

    due_el = (
        item.select_one(".cp-checked-out-due-on")
        or item.select_one(".cp-short-formatted-date")
        or item.select_one(".cp-account-section-date")
        or item.select_one(".due-date")
        or item.select_one("[data-due-date]")
        or item.select_one(".checkout-date")
    )
    if not due_el:
        for el in item.find_all(string=lambda t: t and "Due" in t):
            due_el = el.parent
            break
    due_date = _clean_text(due_el) if due_el else "Unknown"

    description = _first_text(
        item,
        ".display-info-primary",
        ".display-info",
        ".cp-bib-brief-info-description",
        ".cp-bib-brief-description",
        ".cp-bib-description",
        "[data-description]",
        ".description",
        ".summary",
    )
    if description == _first_text(item, ".cp-screen-reader-message"):
        description = ""

    status_el = (
        item.select_one(".status-name")
        or item.select_one(".checkout-status")
        or item.select_one(".cp-checked-out-status-icon")
        or item.select_one(".cp-account-section-status .label")
        or item.select_one(".cp-account-section-status [class*='status']")
        or item.select_one(".cp-account-section-status")
        or item.select_one(".item-status")
        or item.select_one(".status")
        or item.select_one(".overdue")
        or item.select_one(".label-danger")
        or item.select_one(".label-warning")
    )
    status = (
        _remove_text(_clean_text(status_el), description, title, author, due_date)
        if status_el
        else ""
    )
    if not status:
        classes = set(item.get("class") or [])
        status = "Overdue" if "overdue" in classes else "Checked Out"

    renewals_el = (
        item.select_one(".cp-account-section-renewals")
        or item.select_one(".renewals")
    )
    renewals = _clean_text(renewals_el) if renewals_el else ""
    status = _remove_text(status, renewals)

    fmt = _first_text(
        item,
        ".cp-item-format .display-info-primary",
        ".cp-item-format .display-info",
        ".cp-format-info .display-info-primary",
        ".cp-format-info .display-info",
        ".cp-format-indicator",
        ".material-type",
        "[data-format]",
    )
    fmt = fmt.split(",", 1)[0].strip() if fmt else "Book"

    cover_image = ""
    cover_el = (
        item.select_one(".cp-bib-cover img")
        or item.select_one(".cover img")
        or item.select_one(".cp-cover img")
        or item.select_one("img[alt]")
        or item.select_one("img")
    )
    if cover_el:
        for attr in ("src", "data-src", "data-original", "data-lazy-src"):
            value = cover_el.get(attr)
            if value and "blank" not in value.lower() and "placeholder" not in value.lower():
                cover_image = value
                break
    if not cover_image and record_id and cover_map:
        cover_image = cover_map.get(record_id, "")

    return {
        "title": title,
        "author": author,
        "due_date": due_date,
        "description": description,
        "status": status,
        "renewals": renewals,
        "format": fmt,
        "record_id": record_id,
        "book_url": book_url,
        "cover_image": cover_image,
    }
