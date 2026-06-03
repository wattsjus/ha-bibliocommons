# BiblioCommons — Home Assistant Custom Integration

Track checked-out books from a BiblioCommons-powered library directly in Home Assistant. The integration logs in with your library credentials automatically, so there is no cookie hunting required.

[![Add BiblioCommons to Home Assistant](https://my.home-assistant.io/badges/config_flow_start.svg)](https://my.home-assistant.io/redirect/config_flow_start/?domain=bibliocommons)

---

## Installation

### Step 1 — Copy the component

Copy the `custom_components/bibliocommons/` folder into your HA config directory:

```
config/
└── custom_components/
    └── bibliocommons/
        ├── __init__.py
        ├── config_flow.py
        ├── const.py
        ├── manifest.json
        ├── sensor.py
        └── strings.json
```

### Step 2 — Add the integration

1. Restart Home Assistant.
2. Click the **Add BiblioCommons to Home Assistant** button at the top of this README.
3. Enter your library's BiblioCommons catalog URL, library username, and password.
4. Submit the form. Home Assistant will test the login before saving the entry.

> Your credentials are stored in HA's encrypted config entry storage, the same place other integrations store passwords.

---

## How It Works

On each refresh the integration:
1. Reuses the existing authenticated session (no login needed every time)
2. If the session has expired, automatically re-logs in with your credentials
3. Scrapes your checked out page and parses title, author, description, due date, status, and renewals
4. Filters out ebook and other digital checkout formats before creating Home Assistant book records

---

## The Sensor

| Entity | Description |
|--------|-------------|
| `sensor.library_checked_out_books` | Count of currently checked out items |

### Attributes

| Attribute | Description |
|-----------|-------------|
| `books` | Full list: title, author, description, due_date, status, renewals, format, overdue |
| `total_checked_out` | Total number of items |
| `overdue_count` | Number of overdue items |
| `book_titles` | Simple flat list of titles |

---

## Lovelace Card

Paste into your dashboard via the Raw Config Editor:

```yaml
type: markdown
title: 📚 My Library Books
content: >
  {% set books = state_attr('sensor.library_checked_out_books', 'books') %}
  {% set overdue = state_attr('sensor.library_checked_out_books', 'overdue_count') %}
  {% set total = state_attr('sensor.library_checked_out_books', 'total_checked_out') %}

  **{{ total }} book{{ 's' if total != 1 }}** checked out
  {%- if overdue and overdue > 0 %} · ⚠️ **{{ overdue }} overdue**{% endif %}

  ---

  {% if books %}
  {% for book in books %}
  {% if book.overdue %}⚠️{% else %}📖{% endif %} **{{ book.title }}**
  *{{ book.author }}*
  {%- if book.description %}
  {{ book.description }}
  {%- endif %}
  Due: `{{ book.due_date }}`{% if book.renewals %} · {{ book.renewals }}{% endif %}{% if book.status and book.status != 'Checked Out' %} · *{{ book.status }}*{% endif %}

  {% endfor %}
  {% else %}
  *No books currently checked out.*
  {% endif %}
```

## Assign Books to People

After the integration is added:

1. Go to **Settings → Devices & Services**
2. Open **BiblioCommons**
3. Click **Configure**
4. Choose the Home Assistant people who can be assigned books
5. Assign each currently checked-out book to a person

Each book in the `books` attribute will then include:

| Attribute | Description |
|-----------|-------------|
| `book_key` | Stable display key used for assignments |
| `assignee` | Assigned `person.*` entity id |
| `assignee_name` | Friendly person name |
| `assignee_entity_id` | Assigned `person.*` entity id |

Example person dashboard filter:

```yaml
type: markdown
title: Hannah's Library Books
content: >
  {% set books = state_attr('sensor.library_checked_out_books', 'books') %}
  {% for book in books if book.assignee_entity_id == 'person.hannah' %}
  **{{ book.title }}**
  {{ book.status }} · {{ book.due_date }}

  {% else %}
  No books assigned to Hannah.
  {% endfor %}
```

## Custom Book Card

Copy `www/bibliocommons-card.js` into your Home Assistant `www` folder and add this dashboard resource:

```yaml
url: /local/bibliocommons-card.js
type: module
```

Then use the card:

```yaml
type: custom:bibliocommons-card
entity: sensor.library_checked_out_books
title: Library Books
allow_assignment: true
```

For a person dashboard:

```yaml
type: custom:bibliocommons-card
entity: sensor.library_checked_out_books
title: Hannah's Books
assignee_entity_id: person.hannah
allow_assignment: false
```

The card shows the cover on the left when available, title, author, status, due date, and description. Books due within 3 days are yellow; overdue books are red. When `allow_assignment` is `true`, the person dropdown calls `bibliocommons.assign_book`.

---

## Automations

The integration can send daily Home Assistant Companion App notifications to the person assigned to each book. Configure it from:

`Settings -> Devices & services -> BiblioCommons -> Configure`

Enable daily reminders, choose the reminder time, and set the due-soon window. The integration looks for a notify entity tied to each person's device tracker, for example `person.naomi` using `device_tracker.naomis_ipad` will target `notify.naomis_ipad`.

### Notify when a book is due soon
```yaml
automation:
  - alias: "Library Book Due Tomorrow"
    trigger:
      - platform: template
        value_template: >
          {% set books = state_attr('sensor.library_checked_out_books', 'books') %}
          {% for book in books %}
            {% if 'tomorrow' in book.due_date | lower %}true{% endif %}
          {% endfor %}
    action:
      - service: notify.mobile_app_your_phone
        data:
          title: "📚 Library Reminder"
          message: "You have books due tomorrow!"
```

### Notify when anything is overdue
```yaml
automation:
  - alias: "Library Books Overdue"
    trigger:
      - platform: numeric_state
        entity_id: sensor.library_checked_out_books
        attribute: overdue_count
        above: 0
    action:
      - service: notify.mobile_app_your_phone
        data:
          title: "⚠️ Overdue Library Books"
          message: "You have {{ state_attr('sensor.library_checked_out_books', 'overdue_count') }} overdue book(s)!"
```

---

## Troubleshooting

**Sensor shows 0 books but I have checkouts**
The HTML selectors may not match the current BiblioCommons layout. Check HA logs for parsing errors.

**"invalid_auth" error**
Double-check your username and password on your library's BiblioCommons catalog directly.

**"cannot_connect" error**
The library URL may be wrong or the site may be temporarily down.

---

## Update Frequency

Refreshes every **60 minutes**. To change, edit `const.py`:
```python
SCAN_INTERVAL = 30  # minutes
```
