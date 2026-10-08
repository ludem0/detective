"""Загрузка сценария дела и нормализация адресов."""

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

ORDER = "ордер"
SEARCH = "обыск"
_JUNK = re.compile(r"[\s\-–—_.,«»\"']+")
_SLASHES = re.compile(r"/+")
_HOUSE = re.compile(r"\d{1,4}")


class AddressError(ValueError):
    pass


def compact(text: str) -> str:
    """Регистр, ё, пробелы, дефисы и кавычки не важны; дробь важна (В-1/1 ≠ В-11)."""
    text = _JUNK.sub("", text.lower().replace("ё", "е"))
    return _SLASHES.sub("/", text).strip("/")


@dataclass(frozen=True)
class Parsed:
    key: str          # канонический ключ: «П-51/87», «П-00/жд»
    label: str        # как показать игроку
    command: str | None = None   # ORDER | SEARCH | None
    base_key: str = ""           # адрес без команды
    base_label: str = ""


@dataclass(frozen=True)
class Entry:
    id: str
    key: str
    label: str
    title: str
    text: str
    appendices: tuple[int, ...] = ()
    requires: tuple[str, ...] = ()


@dataclass(frozen=True)
class Trigger:
    id: str
    entry: Entry
    delay_sec: int
    after_all: tuple[str, ...]
    after_any: tuple[str, ...]


@dataclass
class Case:
    raw: dict
    districts: tuple[str, ...]
    entries: dict[str, Entry] = field(default_factory=dict)    # ключ → ответ на поездку
    searches: dict[str, Entry] = field(default_factory=dict)   # ключ адреса → результат обыска
    triggers: tuple[Trigger, ...] = ()
    by_id: dict[str, Entry] = field(default_factory=dict)

    @property
    def texts(self) -> dict:
        return self.raw["texts"]

    def parse(self, district: str, number: str) -> Parsed:
        return parse_address(self.districts, district, number)

    def key_of(self, address: str) -> str:
        district, _, rest = address.partition("-")
        return make_key(district, rest)


def make_key(district: str, rest: str) -> str:
    return f"{district.upper()}-{compact(rest)}"


def parse_address(districts: tuple[str, ...], district: str, number: str) -> Parsed:
    text = (number or "").strip()
    if len(text) > 60:
        raise AddressError("Слишком длинный адрес.")
    # Игрок мог вписать адрес целиком: «П-51/87»
    prefix = re.match(r"^([А-Яа-яЁё]{1,2})\s*-\s*(.+)$", text)
    if prefix and prefix.group(1).upper() in districts:
        district, text = prefix.group(1), prefix.group(2)
    district = (district or "").strip().upper()
    if district not in districts:
        raise AddressError("Выберите район.")
    rest = compact(text)
    parts = rest.split("/") if rest else []
    if not parts or not _HOUSE.fullmatch(parts[0]):
        raise AddressError("Номер дома — число, например 96 или 37/504.")
    shown = re.sub(r"\s+", "", text)
    label = f"{district}-{shown}"
    command = parts[-1] if len(parts) > 1 and parts[-1] in (ORDER, SEARCH) else None
    if command:
        base_rest = "/".join(parts[:-1])
        base_shown = shown.rsplit("/", 1)[0]
        return Parsed(
            key=f"{district}-{rest}", label=label, command=command,
            base_key=f"{district}-{base_rest}", base_label=f"{district}-{base_shown}",
        )
    key = f"{district}-{rest}"
    return Parsed(key=key, label=label, base_key=key, base_label=label)


def _entry(case: Case, prefix: str, item: dict, key: str | None = None) -> Entry:
    key = key or case.key_of(item["address"])
    return Entry(
        id=f"{prefix}:{key}",
        key=key,
        label=item["address"],
        title=item["title"],
        text=item["text"].strip(),
        appendices=tuple(item.get("appendices", [])),
        requires=tuple(case.key_of(a) for a in item.get("requires", [])),
    )


def load_case(path: Path) -> Case:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    case = Case(raw=raw, districts=tuple(d["code"] for d in raw["districts"]))
    appendices = raw["appendices"]

    def check(entry: Entry) -> Entry:
        for n in entry.appendices:
            if n not in appendices:
                raise ValueError(f"{entry.label}: нет приложения {n}")
        if entry.id in case.by_id:
            raise ValueError(f"Дубль адреса {entry.label}")
        case.by_id[entry.id] = entry
        return entry

    for item in raw["entries"]:
        entry = check(_entry(case, "v", item))
        case.entries[entry.key] = entry
        for alias in item.get("aliases", []):
            case.entries[case.key_of(alias)] = entry
    for item in raw.get("searches", []):
        entry = check(_entry(case, "s", item))
        case.searches[entry.key] = entry
    triggers = []
    for item in raw.get("triggers", []):
        entry = check(_entry(case, "t", item))
        triggers.append(Trigger(
            id=item["id"],
            entry=entry,
            delay_sec=int(item.get("delay_sec", 60)),
            after_all=tuple(case.key_of(a) for a in item.get("after_all", [])),
            after_any=tuple(case.key_of(a) for a in item.get("after_any", [])),
        ))
    case.triggers = tuple(triggers)
    return case
