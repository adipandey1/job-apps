"""Fill repeating work history and education sections."""

import re
from collections.abc import Callable

from playwright.async_api import Locator, Page

from src.experience_mapper import (
    education_entry_value,
    map_education_field,
    map_work_field,
    work_entry_value,
)

Action = dict[str, str]

REPEATER_FIELD_SELECTOR = (
    "input:visible, textarea:visible, select:visible, "
    "[role=combobox]:visible, [aria-haspopup=listbox]:visible"
)

WORK_ADD_KEYWORDS = (
    "add experience",
    "add employment",
    "add work",
    "add job",
    "add another position",
    "add position",
    "add another",
)
EDUCATION_ADD_KEYWORDS = (
    "add education",
    "add school",
    "add degree",
    "add another education",
    "add another",
)

# Fields where resume-parser autofill routinely truncates or half-fills values
# (e.g. "Director" instead of "Director Data Science and Analytics"). For these
# keys we only skip re-filling when the on-page value is an EXACT match to the
# canonical profile value — a loose substring match is not good enough.
STRICT_MATCH_KEYS = frozenset({"title", "company", "description", "school", "specialization"})


def normalize_value(value: str) -> str:
    return re.sub(r"\s+", " ", value.lower().strip())


def values_match(expected: str, actual: str) -> bool:
    a = normalize_value(expected)
    b = normalize_value(actual)
    if not a or not b:
        return False
    return a == b or a in b or b in a


def find_matching_block_index(
    entry: dict,
    primary_values: list[str],
    secondary_values: list[str],
    primary_key: str,
    secondary_key: str,
    exclude: frozenset[int] = frozenset(),
) -> int | None:
    """Return the page-block index that already represents this profile entry
    (resume autofill or a prior pass), or None if no block matches.

    Matching is position-independent by design: it never assumes block N on
    the page corresponds to entry N in the profile, because resume parsers
    frequently drop an entry's primary field (e.g. leave Company blank) which
    would otherwise desync the two orderings.
    """
    primary = str(entry.get(primary_key, "") or "")
    secondary = str(entry.get(secondary_key, "") or "")

    # Prefer matching by the primary identifier (company/school) when present.
    for index, on_page_primary in enumerate(primary_values):
        if index in exclude:
            continue
        if not (on_page_primary and primary and values_match(primary, on_page_primary)):
            continue
        on_page_secondary = secondary_values[index] if index < len(secondary_values) else ""
        if not secondary or not on_page_secondary or values_match(secondary, on_page_secondary):
            return index

    # Fall back to matching by the secondary identifier alone (title/degree) for
    # blocks where the resume parser left the primary field blank but still
    # captured the title — only for blocks whose primary field is still blank,
    # so we never steal a block that legitimately belongs to another entry.
    if secondary:
        for index, on_page_secondary in enumerate(secondary_values):
            if index in exclude or not on_page_secondary:
                continue
            on_page_primary = primary_values[index] if index < len(primary_values) else ""
            if on_page_primary:
                continue
            if values_match(secondary, on_page_secondary):
                return index

    return None


def find_empty_block_index(
    primary_values: list[str],
    secondary_values: list[str],
    exclude: frozenset[int] = frozenset(),
) -> int | None:
    """An existing block where neither identifying field has any value yet —
    safe to claim for a new entry (e.g. the default starter block on a fresh
    form) without risking overwriting another entry's data."""
    total = max(len(primary_values), len(secondary_values))
    for index in range(total):
        if index in exclude:
            continue
        on_page_primary = primary_values[index] if index < len(primary_values) else ""
        on_page_secondary = secondary_values[index] if index < len(secondary_values) else ""
        if not on_page_primary.strip() and not on_page_secondary.strip():
            return index
    return None


async def read_button_label(element: Locator) -> str:
    from src.filler import safe_attr

    try:
        text = (await element.inner_text()).strip()
    except Exception:
        text = ""
    if text:
        return text
    aria = await safe_attr(element, "aria-label")
    return (aria or "").strip()


async def click_add_button(
    page: Page,
    keywords: tuple[str, ...],
    section_label: str | None = None,
) -> bool:
    from src.filler import safe_count

    # Scope to the specific section's group first — Workday forms commonly label
    # every repeater's add button just "Add"/"Add Another" with no section-specific
    # text, so an unscoped page-wide search can click the WRONG section's button
    # (e.g. add another Work Experience block while trying to add Education).
    if section_label:
        group = page.get_by_role("group", name=section_label, exact=True).first
        try:
            if await safe_count(group) > 0:
                add_btn = group.get_by_role("button", name=re.compile(r"^add\b", re.IGNORECASE))
                if await safe_count(add_btn) > 0:
                    await add_btn.first.scroll_into_view_if_needed()
                    await add_btn.first.click()
                    await page.wait_for_timeout(1500)
                    return True
        except Exception:
            pass

    # Fallback: page-wide keyword search (older/differently-structured forms).
    selectors = ["button:visible", "a:visible", "[role=button]:visible", "input[type=button]:visible"]
    for selector in selectors:
        count = await safe_count(page.locator(selector))
        for i in range(count):
            element = page.locator(selector).nth(i)
            try:
                label = (await read_button_label(element)).lower()
                if any(keyword in label for keyword in keywords):
                    await element.scroll_into_view_if_needed()
                    await element.click()
                    await page.wait_for_timeout(1500)
                    return True
            except Exception:
                continue
    return False


async def read_element_value(element: Locator) -> str:
    from src.filler import safe_count, safe_evaluate

    tag = await safe_evaluate(element, "el => el.tagName.toLowerCase()")
    if not tag:
        return ""
    if tag == "select":
        selected = element.locator("option:checked")
        if await safe_count(selected):
            try:
                return (await selected.first.inner_text()).strip()
            except Exception:
                return ""
        return ""
    try:
        return (await element.input_value()).strip()
    except Exception:
        return ""


async def element_is_empty(element: Locator) -> bool:
    from src.dropdown_fill import is_placeholder_value
    from src.filler import safe_attr

    value = await read_element_value(element)
    input_type = (await safe_attr(element, "type") or "").lower()
    if input_type == "checkbox":
        try:
            return not await element.is_checked()
        except Exception:
            return True
    if is_placeholder_value(value):
        return True
    return not value.strip()


async def page_has_repeater_fields(page: Page, map_func: Callable[[str], str | None]) -> bool:
    from src.filler import accessible_name, safe_count

    count = await safe_count(page.locator(REPEATER_FIELD_SELECTOR))
    for i in range(count):
        element = page.locator(REPEATER_FIELD_SELECTOR).nth(i)
        try:
            if not await safe_count(element):
                continue
            label = await accessible_name(page, element)
            if map_func(label):
                return True
        except Exception:
            continue
    return False


async def get_filled_field_values(
    page: Page,
    map_func: Callable[[str], str | None],
    field_key: str,
) -> list[str]:
    from src.filler import accessible_name, safe_attr, safe_count, safe_evaluate

    values: list[str] = []
    count = await safe_count(page.locator(REPEATER_FIELD_SELECTOR))

    for i in range(count):
        element = page.locator(REPEATER_FIELD_SELECTOR).nth(i)
        try:
            if not await safe_count(element):
                continue

            tag = await safe_evaluate(element, "el => el.tagName.toLowerCase()")
            if not tag:
                continue
            input_type = (await safe_attr(element, "type") or "").lower()
            if tag != "select" and input_type in {"hidden", "submit", "button", "radio", "file", "checkbox"}:
                continue

            label = await accessible_name(page, element)
            key = map_func(label)
            if key != field_key:
                continue

            # Always append (even "") to keep this list positionally aligned with
            # the other field's value list — one entry per block in DOM order.
            # Filtering out blanks here would desync primary/secondary indices
            # whenever a block has one field filled and the other left blank.
            value = await read_element_value(element)
            values.append(value.strip())
        except Exception:
            continue

    return values


async def fill_field_element(
    page: Page,
    element: Locator,
    key: str,
    value: str,
    entry: dict | None = None,
) -> str:
    from src.dropdown_fill import fill_dropdown_field
    from src.filler import fill_text_field, safe_attr, safe_evaluate

    tag = await safe_evaluate(element, "el => el.tagName.toLowerCase()")
    role = await safe_attr(element, "role")
    haspopup = await safe_attr(element, "aria-haspopup")

    is_dropdown = (
        tag == "select"
        or role == "combobox"
        or haspopup in {"listbox", "menu"}
        or key in {"degree", "specialization", "field_of_study"}
    )

    if is_dropdown:
        return await fill_dropdown_field(page, element, key, value, entry=entry)

    return await fill_text_field(page, element, str(value))


async def fill_entry_fields(
    page: Page,
    entry: dict,
    map_func: Callable[[str], str | None],
    value_func: Callable[[dict, str], str | bool],
    section: str,
    entry_index: int,
) -> tuple[list[Action], list[Action]]:
    from collections import defaultdict

    from src.filler import accessible_name, safe_attr, safe_count, safe_evaluate

    filled: list[Action] = []
    skipped: list[Action] = []
    key_occurrence: dict[str, int] = defaultdict(int)

    count = await safe_count(page.locator(REPEATER_FIELD_SELECTOR))

    for i in range(count):
        element = page.locator(REPEATER_FIELD_SELECTOR).nth(i)
        try:
            if not await safe_count(element):
                continue

            tag = await safe_evaluate(element, "el => el.tagName.toLowerCase()")
            if not tag:
                continue
            input_type = (await safe_attr(element, "type") or "").lower()
            if tag != "select" and input_type in {"hidden", "submit", "button", "radio", "file"}:
                continue

            label = await accessible_name(page, element)
            key = map_func(label)
            if not key:
                continue

            occurrence = key_occurrence[key]
            key_occurrence[key] += 1

            # Fill only the Nth block — entry 0 → 0th degree field, entry 1 → 1st degree field.
            if occurrence != entry_index:
                continue

            value = value_func(entry, key)
            if key != "current" and (value is None or value == "" or value is False):
                continue

            if key != "current":
                current = await read_element_value(element)
                if current.strip():
                    if key in STRICT_MATCH_KEYS:
                        # Resume parsers routinely truncate these (e.g. "Director"
                        # instead of "Director Data Science and Analytics") — only
                        # skip when it's already an exact match to the canonical value.
                        if normalize_value(current) == normalize_value(str(value)):
                            continue
                    elif values_match(str(value), current):
                        continue

            field_key = f"{section}[{entry_index}].{key}"
            if key == "current":
                if value:
                    await element.check()
                    filled.append({"label": label, "key": field_key, "value": "checked"})
                else:
                    await element.uncheck()
                    filled.append({"label": label, "key": field_key, "value": "unchecked"})
            else:
                filled_value = await fill_field_element(page, element, key, str(value), entry=entry)
                filled.append({"label": label, "key": field_key, "value": filled_value})
        except Exception as exc:
            skipped.append(
                {
                    "label": f"{section}[{entry_index}]",
                    "key": f"{section}[{entry_index}]",
                    "error": str(exc),
                }
            )

    return filled, skipped


async def fill_repeater_section(
    page: Page,
    profile: dict,
    section: str,
    map_func: Callable[[str], str | None],
    value_func: Callable[[dict, str], str | bool],
    add_keywords: tuple[str, ...],
    primary_key: str,
    secondary_key: str,
    group_label: str | None = None,
) -> tuple[list[Action], list[Action]]:
    entries = profile.get(section, [])
    if not entries:
        return [], []

    if not await page_has_repeater_fields(page, map_func):
        return [], []

    filled: list[Action] = []
    skipped: list[Action] = []
    claimed_blocks: set[int] = set()

    for entry in entries:
        primary_values = await get_filled_field_values(page, map_func, primary_key)
        secondary_values = await get_filled_field_values(page, map_func, secondary_key)

        block_index = find_matching_block_index(
            entry, primary_values, secondary_values, primary_key, secondary_key,
            exclude=frozenset(claimed_blocks),
        )

        if block_index is None:
            # No block represents this entry yet. Reuse an untouched empty block
            # (e.g. the default starter block on a fresh form) before creating a
            # new one — never guess by position, which risks overwriting a
            # different entry's data (e.g. after a block was deleted, leaving a
            # gap that doesn't line up with profile order anymore).
            block_index = find_empty_block_index(
                primary_values, secondary_values, exclude=frozenset(claimed_blocks)
            )

        if block_index is None:
            added = await click_add_button(page, add_keywords, section_label=group_label)
            if not added:
                skipped.append(
                    {
                        "label": f"{entry.get(primary_key, '')} / {entry.get(secondary_key, '')}".strip(" /"),
                        "key": section,
                        "error": "could not find Add button for new entry",
                    }
                )
                continue
            primary_values = await get_filled_field_values(page, map_func, primary_key)
            block_index = len(primary_values) - 1 if primary_values else 0

        claimed_blocks.add(block_index)
        part_filled, part_skipped = await fill_entry_fields(
            page, entry, map_func, value_func, section, block_index
        )
        filled.extend(part_filled)
        skipped.extend(part_skipped)

    return filled, skipped


async def fill_work_history(page: Page, profile: dict) -> tuple[list[Action], list[Action]]:
    return await fill_repeater_section(
        page,
        profile,
        "work_history",
        map_work_field,
        work_entry_value,
        WORK_ADD_KEYWORDS,
        primary_key="company",
        secondary_key="title",
        group_label="Work Experience",
    )


async def fill_education(page: Page, profile: dict) -> tuple[list[Action], list[Action]]:
    return await fill_repeater_section(
        page,
        profile,
        "education",
        map_education_field,
        education_entry_value,
        EDUCATION_ADD_KEYWORDS,
        primary_key="school",
        secondary_key="degree",
        group_label="Education",
    )
