"""Sensor platform for BiblioCommons integration."""
from __future__ import annotations

import logging
import re
from typing import Any

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    DOMAIN,
    CONF_LIBRARY_URL,
    CONF_LIBRARY_ICON_URL,
    CONF_LIBRARY_CARD_NUMBER,
    CONF_BOOK_ASSIGNMENTS,
    CONF_BOOK_HISTORY,
    CONF_BOOK_REPORTS,
)
from . import BiblioCommonsCoordinator

_LOGGER = logging.getLogger(__name__)


def _book_key(book: dict[str, Any]) -> str:
    """Create the display key used for book assignments."""
    title = book.get("title") or "Unknown Title"
    author = book.get("author") or ""
    return f"{title} - {author}" if author else title


def _normalize_match_text(value: str) -> str:
    """Normalize scraped book text for resilient assignment matching."""
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).strip()


def _title_from_book_key(book_key: str) -> str:
    """Return a readable title from an assignment key."""
    return book_key.rsplit(" - ", 1)[0]


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


def _clean_book_assignments(raw_assignments) -> dict[str, list[str]]:
    """Normalize stored assignment data."""
    if not isinstance(raw_assignments, dict):
        return {}
    assignments = {}
    for key, value in raw_assignments.items():
        people = _assignment_person_ids(value)
        if people:
            assignments[str(key)] = people
    return assignments


def _assignment_match_indexes(
    assignments: dict[str, list[str]],
) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    """Build unique fuzzy lookup indexes for stored assignments."""
    full_candidates: dict[str, list[str]] = {}
    title_candidates: dict[str, list[str]] = {}
    for book_key in assignments:
        full_token = _normalize_match_text(book_key)
        title_token = _normalize_match_text(_title_from_book_key(book_key))
        if full_token:
            full_candidates.setdefault(full_token, []).append(book_key)
        if title_token:
            title_candidates.setdefault(title_token, []).append(book_key)
    return (
        {
            token: assignments[keys[0]]
            for token, keys in full_candidates.items()
            if len(set(keys)) == 1
        },
        {
            token: assignments[keys[0]]
            for token, keys in title_candidates.items()
            if len(set(keys)) == 1
        },
    )


def _book_assignee(
    book: dict[str, Any],
    book_key: str,
    assignments: dict[str, list[str]],
    full_index: dict[str, list[str]],
    title_index: dict[str, list[str]],
) -> list[str]:
    """Resolve assignees for a current book, tolerating key text drift."""
    if assignees := assignments.get(book_key, []):
        return assignees
    full_token = _normalize_match_text(book_key)
    title_token = _normalize_match_text(book.get("title", ""))
    return full_index.get(full_token, []) or title_index.get(title_token, [])


def _book_is_active_history_item(
    item: dict[str, Any],
    active_full_tokens: set[str],
    active_title_tokens: set[str],
) -> bool:
    """Return true if a history entry still appears in active checkouts."""
    full_token = _normalize_match_text(item.get("book_key", ""))
    title_token = _normalize_match_text(item.get("title", ""))
    return (
        bool(full_token and full_token in active_full_tokens)
        or bool(title_token and title_token in active_title_tokens)
    )


def _reports_for_book(
    book_key: str,
    reports: list[dict[str, Any]],
    person_entity_id: str = "",
) -> list[dict[str, Any]]:
    """Return reports for the given book key, optionally limited to a person."""
    person = person_entity_id if person_entity_id.startswith("person.") else ""
    return [
        report
        for report in reports
        if (not person or report.get("person_entity_id") == person)
        and (
            report.get("book_key") == book_key
            or _normalize_match_text(report.get("book_key", ""))
            == _normalize_match_text(book_key)
            or _normalize_match_text(_title_from_book_key(report.get("book_key", "")))
            == _normalize_match_text(_title_from_book_key(book_key))
        )
    ]


def _assignee_name(hass: HomeAssistant, assignee: str) -> str:
    """Resolve an assignment value to a display name."""
    state = hass.states.get(assignee)
    return state.attributes.get("friendly_name", state.name) if state else ""


def _assignee_names(hass: HomeAssistant, assignees: list[str]) -> list[str]:
    """Resolve assignment person ids to display names."""
    return [_assignee_name(hass, assignee) for assignee in assignees]


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


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up BiblioCommons sensors from a config entry."""
    coordinator: BiblioCommonsCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([BiblioCommonsCheckedOutSensor(coordinator, entry)], True)


class BiblioCommonsCheckedOutSensor(CoordinatorEntity, SensorEntity):
    """Sensor representing all checked out books."""

    def __init__(
        self,
        coordinator: BiblioCommonsCoordinator,
        entry: ConfigEntry,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_checked_out"
        self._attr_name = "Library Checked Out Books"
        self._attr_icon = "mdi:book-open-page-variant"

    @property
    def native_value(self) -> int:
        """Return the number of checked out items."""
        if self.coordinator.data is None:
            return 0
        return len(self.coordinator.data)

    @property
    def native_unit_of_measurement(self) -> str:
        """Return unit."""
        return "books"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the full list of books as attributes."""
        books = self.coordinator.data or []

        # Build a clean list for display
        book_list = []
        overdue_count = 0
        assignee_ids = _person_entity_ids(self.hass)
        assignments = _clean_book_assignments(
            self._entry.options.get(CONF_BOOK_ASSIGNMENTS, {})
        )
        assignment_full_index, assignment_title_index = _assignment_match_indexes(assignments)
        reports = [
            report
            for report in self._entry.options.get(CONF_BOOK_REPORTS, [])
            if isinstance(report, dict)
        ]
        active_full_tokens = {_normalize_match_text(_book_key(book)) for book in books}
        active_title_tokens = {
            _normalize_match_text(book.get("title", ""))
            for book in books
            if book.get("title")
        }
        assignees = [
            {
                "value": assignee,
                "name": _assignee_name(self.hass, assignee),
                "entity_id": assignee if assignee.startswith("person.") else "",
            }
            for assignee in assignee_ids
        ]
        history = []
        for item in self._entry.options.get(CONF_BOOK_HISTORY, []):
            if not isinstance(item, dict):
                continue
            if _book_is_active_history_item(item, active_full_tokens, active_title_tokens):
                continue
            assignee = item.get("assignee_entity_id", item.get("assignee", ""))
            item_reports = _reports_for_book(item.get("book_key", ""), reports, str(assignee))
            history.append({
                "book_key": item.get("book_key", ""),
                "title": item.get("title", "Unknown Title"),
                "author": item.get("author", ""),
                "assignee_name": item.get("assignee_name", "")
                or (_assignee_name(self.hass, assignee) if assignee else ""),
                "assignee_entity_id": assignee if str(assignee).startswith("person.") else "",
                "returned_at": item.get("returned_at", ""),
                "lexile_level": item.get("lexile_level", ""),
                "reading_level": item.get("reading_level", ""),
                "reading_level_source": item.get("reading_level_source", ""),
                "library_name": item.get("library_name", self._entry.title),
                "library_url": item.get("library_url", self._entry.data.get(CONF_LIBRARY_URL, "")),
                "book_reports": item_reports,
                "book_report_count": len(item_reports),
            })

        for book in books:
            status = book.get("status", "Checked Out")
            is_overdue = "overdue" in status.lower()
            if is_overdue:
                overdue_count += 1
            book_key = _book_key(book)
            assignees_for_book = _book_assignee(
                book,
                book_key,
                assignments,
                assignment_full_index,
                assignment_title_index,
            )
            assignee_names = _assignee_names(self.hass, assignees_for_book)

            book_reports = _reports_for_book(book_key, reports)
            book_list.append({
                "book_key": book_key,
                "title": book.get("title", "Unknown Title"),
                "author": book.get("author", ""),
                "due_date": book.get("due_date", ""),
                "description": book.get("description", ""),
                "status": status,
                "renewals": book.get("renewals", ""),
                "format": book.get("format", "Book"),
                "record_id": book.get("record_id", ""),
                "book_url": book.get("book_url", ""),
                "cover_image": book.get("cover_image", ""),
                "lexile_level": book.get("lexile_level", ""),
                "reading_level": book.get("reading_level", ""),
                "reading_level_source": book.get("reading_level_source", ""),
                "overdue": is_overdue,
                "assignee": assignees_for_book[0] if assignees_for_book else "",
                "assignee_name": ", ".join(assignee_names),
                "assignee_entity_id": assignees_for_book[0] if assignees_for_book else "",
                "assignees": assignees_for_book,
                "assignee_names": assignee_names,
                "assignee_entity_ids": assignees_for_book,
                "book_reports": book_reports,
                "book_report_count": len(book_reports),
            })

        # Sort unassigned first, then keep the existing due-date priority.
        book_list.sort(
            key=lambda b: (
                bool(b.get("assignee_entity_ids")),
                not b["overdue"],
                b.get("due_date", ""),
            )
        )

        return {
            "library_name": self._entry.title,
            "library_url": self._entry.data.get(CONF_LIBRARY_URL, ""),
            "library_favicon": self._entry.data.get(CONF_LIBRARY_ICON_URL, "")
            or f"{self._entry.data.get(CONF_LIBRARY_URL, '').rstrip('/')}/favicon.ico",
            "library_card_number": getattr(self.coordinator, "library_card_number", "")
            or self._entry.options.get(CONF_LIBRARY_CARD_NUMBER, "")
            or self._entry.data.get(CONF_LIBRARY_CARD_NUMBER, ""),
            "config_entry_id": self._entry.entry_id,
            "books": book_list,
            "total_checked_out": len(book_list),
            "overdue_count": overdue_count,
            "book_titles": [b["title"] for b in book_list],
            "assignees": assignees,
            "reading_history": history,
            "book_reports": reports,
        }
