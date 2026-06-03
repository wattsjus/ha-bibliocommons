"""Config flow for BiblioCommons integration."""
from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import selector

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
    CONF_PENDING_MISSING_BOOKS,
    CONF_PENDING_MISSING_AT,
    CONF_DAILY_REMINDERS,
    CONF_REMINDER_TIME,
    CONF_DUE_SOON_DAYS,
    DEFAULT_DAILY_REMINDERS,
    DEFAULT_REMINDER_TIME,
    DEFAULT_DUE_SOON_DAYS,
)

_LOGGER = logging.getLogger(__name__)

STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_LIBRARY_URL, default="https://your-library.bibliocommons.com"): str,
        vol.Required(CONF_USERNAME): str,
        vol.Required(CONF_PASSWORD): str,
    }
)


class BiblioCommonsConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for BiblioCommons."""

    VERSION = 1

    @staticmethod
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> BiblioCommonsOptionsFlow:
        """Create the options flow."""
        return BiblioCommonsOptionsFlow(config_entry)

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle the initial step."""
        errors: dict[str, str] = {}

        if user_input is not None:
            library_url = user_input[CONF_LIBRARY_URL].rstrip("/")
            username = user_input[CONF_USERNAME].strip()
            password = user_input[CONF_PASSWORD]

            try:
                from . import bibliocommons_login, bibliocommons_site_metadata
                session = await self.hass.async_add_executor_job(
                    bibliocommons_login, library_url, username, password
                )
                site_metadata = await self.hass.async_add_executor_job(
                    bibliocommons_site_metadata, library_url, session
                )
                entry_title = site_metadata.get("name", "Biblio Commons")
                # Login succeeded — save entry
                return self.async_create_entry(
                    title=entry_title,
                    data={
                        CONF_LIBRARY_URL: library_url,
                        CONF_LIBRARY_ICON_URL: site_metadata.get("icon_url", ""),
                        CONF_USERNAME: username,
                        CONF_PASSWORD: password,
                    },
                )
            except ValueError:
                errors["base"] = "invalid_auth"
            except Exception as err:  # noqa: BLE001
                _LOGGER.exception("Unexpected error during login: %s", err)
                errors["base"] = "cannot_connect"

        return self.async_show_form(
            step_id="user",
            data_schema=STEP_USER_DATA_SCHEMA,
            errors=errors,
        )


def _book_key(book: dict[str, Any]) -> str:
    """Create the display key used for book assignments."""
    title = book.get("title") or "Unknown Title"
    author = book.get("author") or ""
    return f"{title} - {author}" if author else title


def _person_options(hass) -> list[dict[str, str]]:
    """Return Home Assistant person entities as selector options."""
    people_by_entity_id = {}
    registry = er.async_get(hass)
    for entry in registry.entities.values():
        if entry.domain != "person" or getattr(entry, "disabled_by", None):
            continue
        state = hass.states.get(entry.entity_id)
        name = (
            entry.name
            or (state.attributes.get("friendly_name") if state else None)
            or entry.original_name
            or entry.entity_id
        )
        people_by_entity_id[entry.entity_id] = {
            "value": entry.entity_id,
            "label": name,
        }

    for state in hass.states.async_all("person"):
        if state.entity_id in people_by_entity_id:
            continue
        people_by_entity_id[state.entity_id] = {
            "value": state.entity_id,
            "label": state.attributes.get("friendly_name", state.name),
        }

    people = list(people_by_entity_id.values())
    people.sort(key=lambda item: item["label"].lower())
    return people


def _assignee_options(hass, assignees: list[str]) -> list[dict[str, str]]:
    """Build assignment dropdown options."""
    options = [{"value": "", "label": "Unassigned"}]
    labels = {option["value"]: option["label"] for option in _person_options(hass)}
    seen = set()
    for assignee in assignees:
        if not assignee or assignee in seen:
            continue
        seen.add(assignee)
        if assignee in labels:
            options.append({"value": assignee, "label": labels[assignee]})
    return options


def _all_person_entity_ids(hass) -> list[str]:
    """Return all enabled Home Assistant person entity ids."""
    return [option["value"] for option in _person_options(hass)]


class BiblioCommonsOptionsFlow(config_entries.OptionsFlow):
    """Handle BiblioCommons options."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        """Initialize options flow."""
        self._config_entry = config_entry
        self._assignees: list[str] = []
        self._daily_reminders = config_entry.options.get(
            CONF_DAILY_REMINDERS, DEFAULT_DAILY_REMINDERS
        )
        self._reminder_time = config_entry.options.get(
            CONF_REMINDER_TIME, DEFAULT_REMINDER_TIME
        )
        self._due_soon_days = config_entry.options.get(
            CONF_DUE_SOON_DAYS, DEFAULT_DUE_SOON_DAYS
        )

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Collect person entities."""
        if user_input is not None:
            selected = user_input.get(CONF_ALERT_PEOPLE, [])
            self._assignees = [
                assignee
                for assignee in selected
                if isinstance(assignee, str) and assignee.startswith("person.")
            ]
            self._daily_reminders = user_input.get(
                CONF_DAILY_REMINDERS, DEFAULT_DAILY_REMINDERS
            )
            self._reminder_time = user_input.get(
                CONF_REMINDER_TIME, DEFAULT_REMINDER_TIME
            )
            self._due_soon_days = user_input.get(
                CONF_DUE_SOON_DAYS, DEFAULT_DUE_SOON_DAYS
            )
            return await self.async_step_assignments()

        person_options = _person_options(self.hass)
        if not self._assignees:
            if CONF_ALERT_PEOPLE in self._config_entry.options:
                self._assignees = [
                    assignee
                    for assignee in self._config_entry.options.get(CONF_ALERT_PEOPLE, [])
                    if isinstance(assignee, str) and assignee.startswith("person.")
                ]
            elif CONF_ASSIGNEES in self._config_entry.options:
                self._assignees = [
                    assignee
                    for assignee in self._config_entry.options.get(CONF_ASSIGNEES, [])
                    if isinstance(assignee, str) and assignee.startswith("person.")
                ]
            else:
                self._assignees = [option["value"] for option in person_options]
        data_schema = vol.Schema(
            {
                vol.Optional(
                    CONF_ALERT_PEOPLE,
                    default=self._assignees,
                ): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=person_options,
                        multiple=True,
                        mode=selector.SelectSelectorMode.LIST,
                    )
                ),
                vol.Optional(
                    CONF_DAILY_REMINDERS,
                    default=self._daily_reminders,
                ): selector.BooleanSelector(),
                vol.Optional(
                    CONF_REMINDER_TIME,
                    default=self._reminder_time,
                ): selector.TimeSelector(),
                vol.Optional(
                    CONF_DUE_SOON_DAYS,
                    default=self._due_soon_days,
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=0,
                        max=14,
                        step=1,
                        mode=selector.NumberSelectorMode.BOX,
                    )
                ),
            }
        )

        return self.async_show_form(
            step_id="init",
            data_schema=data_schema,
        )

    async def async_step_assignments(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Assign current checkouts to people."""
        all_assignees = _all_person_entity_ids(self.hass)
        existing_assignments = dict(
            self._config_entry.options.get(CONF_BOOK_ASSIGNMENTS, {})
        )
        existing_assignments = {
            book_key: (
                assignee
                if isinstance(assignee, str)
                else next((item for item in assignee if item in all_assignees), "")
            )
            for book_key, assignee in existing_assignments.items()
            if (
                isinstance(assignee, str)
                and assignee in all_assignees
            )
            or (
                isinstance(assignee, list)
                and any(item in all_assignees for item in assignee)
            )
        }
        coordinator = self.hass.data.get(DOMAIN, {}).get(self._config_entry.entry_id)
        books = coordinator.data if coordinator and coordinator.data else []

        if user_input is not None:
            assignments = existing_assignments
            for book in books:
                key = _book_key(book)
                assignee = user_input.get(key, "")
                if assignee in all_assignees:
                    assignments[key] = assignee
                else:
                    assignments.pop(key, None)

            return self.async_create_entry(
                title="",
                data={
                    CONF_ALERT_PEOPLE: self._assignees,
                    CONF_DAILY_REMINDERS: self._daily_reminders,
                    CONF_REMINDER_TIME: self._reminder_time,
                    CONF_DUE_SOON_DAYS: self._due_soon_days,
                    CONF_BOOK_ASSIGNMENTS: assignments,
                    CONF_BOOK_HISTORY: self._config_entry.options.get(
                        CONF_BOOK_HISTORY,
                        [],
                    ),
                    CONF_BOOK_REPORTS: self._config_entry.options.get(
                        CONF_BOOK_REPORTS,
                        [],
                    ),
                    CONF_LAST_BOOKS: self._config_entry.options.get(
                        CONF_LAST_BOOKS,
                        [],
                    ),
                    CONF_LIBRARY_CARD_NUMBER: self._config_entry.options.get(
                        CONF_LIBRARY_CARD_NUMBER,
                        "",
                    ),
                    CONF_PENDING_MISSING_BOOKS: self._config_entry.options.get(
                        CONF_PENDING_MISSING_BOOKS,
                        [],
                    ),
                    CONF_PENDING_MISSING_AT: self._config_entry.options.get(
                        CONF_PENDING_MISSING_AT,
                        "",
                    ),
                },
            )

        if not books:
            return self.async_create_entry(
                title="",
                data={
                    CONF_ALERT_PEOPLE: self._assignees,
                    CONF_DAILY_REMINDERS: self._daily_reminders,
                    CONF_REMINDER_TIME: self._reminder_time,
                    CONF_DUE_SOON_DAYS: self._due_soon_days,
                    CONF_BOOK_ASSIGNMENTS: existing_assignments,
                    CONF_BOOK_HISTORY: self._config_entry.options.get(
                        CONF_BOOK_HISTORY,
                        [],
                    ),
                    CONF_BOOK_REPORTS: self._config_entry.options.get(
                        CONF_BOOK_REPORTS,
                        [],
                    ),
                    CONF_LAST_BOOKS: self._config_entry.options.get(
                        CONF_LAST_BOOKS,
                        [],
                    ),
                    CONF_LIBRARY_CARD_NUMBER: self._config_entry.options.get(
                        CONF_LIBRARY_CARD_NUMBER,
                        "",
                    ),
                    CONF_PENDING_MISSING_BOOKS: self._config_entry.options.get(
                        CONF_PENDING_MISSING_BOOKS,
                        [],
                    ),
                    CONF_PENDING_MISSING_AT: self._config_entry.options.get(
                        CONF_PENDING_MISSING_AT,
                        "",
                    ),
                },
            )

        assignee_options = _assignee_options(self.hass, all_assignees)
        schema_fields = {}
        for book in books:
            key = _book_key(book)
            schema_fields[
                vol.Optional(key, default=existing_assignments.get(key, ""))
            ] = selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=assignee_options,
                    mode=selector.SelectSelectorMode.DROPDOWN,
                )
            )

        return self.async_show_form(
            step_id="assignments",
            data_schema=vol.Schema(schema_fields),
        )
