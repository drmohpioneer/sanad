"""Owns the one fact Sanad reads from outside the doctor's records: a map.

The Resolver (core/resolver.py) needs to answer "the lab in my area is closed"
with somewhere the patient can actually go. That answer cannot be generated: a
lab a model invented is worse than no answer at all, because the patient will
travel to it. So there is one function here, `search`, it calls Google's Places
API (New) Text Search, and everything a patient reads about a place is a field
that API returned, copied, never phrased.

Three properties this file has to have, and each one is a defect it prevents:

  1. **It fails soft, always.** No `MAPS_API_KEY`, a network error, a quota, a
     malformed payload: every one of them comes back as a `Search` carrying an
     `error` and no places at all, and the Resolver hands the barrier to the
     doctor with that sentence in `tried`. Nothing here raises at the caller.
     Without this, a missing key on the demo machine is a 500 on the patient's
     page rather than a card the doctor can act on.

  2. **It never invents a place, and it never claims a price.** Maps has no lab
     prices. `cheap=True` only changes the words of the query, so what comes
     back is public and government laboratories, which are usually cheaper in
     Egypt, and the sentence the patient reads says exactly that and no number.
     There is no field on `Place` a price could be written into.

  3. **A place is data from outside.** A display name and an address are typed
     by whoever owns that listing, so both are cleaned in code before they can
     reach a patient: control characters dropped, whitespace collapsed, length
     capped, and dashes turned into words, because a message Sanad sends is
     written the way Sanad writes. A listing whose name or address reads like a
     dose is refused outright (`safe` below): it is far-fetched, and it is one
     line, and the one thing this system must never do is put a dose in front
     of a patient that the doctor did not write.

The Maps link is built here from the place id rather than taken from the
payload, so a URL in the response cannot become a URL Sanad sends.

**There is no distance.** Text Search is given an area name ("Nasr City"),
because an area name is what a patient types and what the doctor dictates;
Sanad never holds a patient's coordinates and will not ask for them. Without a
patient location there is no distance to compute and none is printed, so
"nearest" in the transport route means "inside the area he named" and says so.

`httpx` is imported inside the coroutine that needs it, exactly as the cloud
SDKs are elsewhere in core/, so this module and its tests import anywhere.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any, Optional

from . import timing

log = logging.getLogger("sanad.places")

ENDPOINT = "https://places.googleapis.com/v1/places:searchText"

# Only what is printed to the patient is asked for. A field mask is required by
# this API, and a narrow one is also the cheapest request and the smallest
# payload that can carry something unexpected.
FIELD_MASK = ",".join((
    "places.id",
    "places.displayName",
    "places.formattedAddress",
    "places.currentOpeningHours.openNow",
    "places.regularOpeningHours.weekdayDescriptions",
))

# Three is what a patient can act on. A list of ten is a second problem.
LIMIT = 3
TIMEOUT_SECONDS = float(os.environ.get("MAPS_TIMEOUT", "10"))

# The two things Sanad may look for. "clinic" is deliberately not one of them:
# sending a patient of this doctor to a different doctor is a referral, and a
# referral is a clinical decision with no tool in this system. A visit that has
# become difficult is moved to another day instead (core/resolver.py).
KINDS: tuple[str, ...] = ("lab", "pharmacy")

# The words that make the query. `cheap` swaps in the public sector, which is
# where a test costs less in Egypt; it is a query, never a claim about a price.
QUERIES: dict[tuple[str, bool], str] = {
    ("lab", False): "medical laboratory in {area}",
    ("lab", True): "government hospital medical laboratory in {area}",
    ("pharmacy", False): "pharmacy in {area}",
    ("pharmacy", True): "government pharmacy in {area}",
}

NO_KEY = "search unavailable: no MAPS_API_KEY on this deployment"

# The hermetic unittest boundary (app/sanad_test_guard.py) refuses every socket
# in the process and raises a BaseException doing it, which the `except
# Exception` at the bottom of `search` could never catch: an unmocked search in
# the suite would not be a soft failure, it would be a crash with a traceback
# from the guard. So the probe is read first and the search never starts, the
# same way core/auditor.py and core/steward.py read `_sanad_hermetic` off the
# model client before they call it. A suite with no network behaves exactly
# like a deployment with no key, which is a supported state everywhere above.
NO_NETWORK = "search unavailable: no network on this process"

# The second search, when the first one found nothing.
#
# A first search that comes back empty is not the end of what Sanad can do, and
# the difference between an agent and a script is what happens next. So there
# is a second attempt, it is decided by the table below and by `widen`, and it
# is code: the model is not asked to try again and never sees either result.
#
# The order is what a person would do. Drop "open now" first, because a
# laboratory that is closed this evening is still one he can go to tomorrow and
# it is a real answer. Then drop the public-sector bias, because any laboratory
# he can reach beats none at all, and the sentence he reads changes with it, so
# he is never told a private laboratory is a cheap one. Only then widen the
# area, and only where a wider area is a real place.
#
# The gazetteer is deliberately small and deliberately honest: districts of
# Greater Cairo, and the three Delta cities whose governorate has a different
# name. An area that is not in it has no wider area, `widen` says so, and the
# hand-over card prints that instead of a second search of somewhere invented.
# Nothing here guesses at geography.
WIDER: dict[str, str] = {
    "nasr city": "Cairo", "heliopolis": "Cairo", "maadi": "Cairo",
    "shubra": "Cairo", "shoubra": "Cairo", "dokki": "Cairo",
    "mohandessin": "Cairo", "helwan": "Cairo", "new cairo": "Cairo",
    "zamalek": "Cairo", "abbassia": "Cairo", "ain shams": "Cairo",
    "mokattam": "Cairo", "giza": "Cairo",
    "sixth of october city": "Cairo", "sixth of october": "Cairo",
    "zagazig": "Sharqia", "mansoura": "Dakahlia", "tanta": "Gharbia",
}

# A field long enough to hide something in is a field nobody reads anyway.
MAX_FIELD = 120

# A listing name or address that reads like a dose never reaches a patient. It
# is a number beside a unit, or the word dose itself, in either language. Bare
# words a real business uses ("take", "care", "24") are deliberately not here:
# a rail that refuses real laboratories is a rail that quietly sends the doctor
# a hand-over he did not need.
DOSE_LIKE = re.compile(r"\d\s*(mg|mcg|ug|ml|iu)\b|\bdoses?\b|جرعة", re.IGNORECASE)

# Google writes opening hours with an en dash between the two times, and a
# dash of any width is how a machine writes. Sanad writes the word "to", so
# every one of them is replaced on the way in.
# Written as escapes and not as characters, because a rail in the suite fails
# the build on a literal em or en dash anywhere in a shipped file, and this is
# the one place that has to name them in order to remove them.
_DASHES = ("\u2014", "\u2013", "\u2012", "\u2011", "\u2010")


@dataclass(frozen=True)
class Place:
    """One place, exactly as the API described it. Nothing here is judged.

    `hours` is that listing's own printed line for today and nothing else: a
    week of opening hours is not an answer to "where can I go now". `open_now`
    is None when the listing publishes no hours at all, which is common, and
    None is printed as nothing rather than as "closed".
    """

    name: str
    address: str = ""
    open_now: Optional[bool] = None
    hours: str = ""
    link: str = ""

    def line(self) -> str:
        """The two lines a patient reads about this place. Code, never a model."""
        head = self.name if not self.address else f"{self.name}, {self.address}"
        under = [self.hours] if self.hours else []
        if self.open_now is True:
            under.insert(0, "open now")
        elif self.open_now is False:
            under.insert(0, "closed now")
        second = " · ".join([*under, self.link] if self.link else under)
        return f"{head}\n   {second}" if second else head


@dataclass(frozen=True)
class Search:
    """What one search produced: places, or the reason there are none.

    `error` is the whole fail-soft contract. It is empty when the search ran,
    and it carries a sentence a doctor can read when it did not, which is what
    the Resolver puts in `tried` on the hand-over card.
    """

    query: str = ""
    places: tuple[Place, ...] = ()
    error: str = ""

    @property
    def unavailable(self) -> bool:
        return bool(self.error)

    def __len__(self) -> int:
        return len(self.places)

    def block(self) -> str:
        """The places, numbered, as one block of text under a template."""
        return "\n".join(f"{i}. {place.line()}"
                         for i, place in enumerate(self.places, start=1))

    def tried(self) -> str:
        """One line for the hand-over card: what was searched and what came back."""
        if self.error:
            return f"Searched for {self.query}: {self.error}."
        if not self.places:
            return f"Searched for {self.query}: nothing found."
        return f"Searched for {self.query}: {len(self.places)} found."


def _hermetic() -> bool:
    """True inside the hermetic unittest process, where no socket exists.

    One attribute is read off the boundary's own GenAI double and nothing else
    on it is touched, exactly as core/auditor._model_ready reads it. Anything
    unreadable is False, which leaves the ordinary path and its own fail-soft
    in charge; the guard is still there underneath either way.
    """
    try:
        from . import media

        return getattr(media.client, "_sanad_hermetic", False) is True
    except Exception:  # noqa: BLE001 - an unreadable probe is not a verdict
        return False


def configured() -> bool:
    """Is there a key at all? Reported by /health so it is never a guess."""
    return bool(os.environ.get("MAPS_API_KEY", "").strip())


def query_for(kind: str, area: str, cheap: bool = False) -> str:
    """The words sent to Text Search. A table, so no model writes the query."""
    shape = QUERIES.get((kind, bool(cheap)))
    if shape is None:
        return ""
    return shape.format(area=" ".join((area or "").split()))


NO_WIDER = "no wider area is known for {area}"


def widen(area: str, *, open_now: bool, cheap: bool) -> Optional[dict[str, Any]]:
    """The second search to make when the first found nothing, or None.

    Pure, and a table: given what the first attempt asked for, this is what the
    next one asks for. It relaxes exactly one thing at a time and in the order
    a person would relax it (see the note beside `WIDER`), so the second search
    is always a real widening of the first and never a different question.

    None means there is nothing left to relax and nothing wider that Sanad can
    honestly name, which is when the barrier goes to the doctor.
    """
    area = " ".join((area or "").split())
    if open_now:
        return {"area": area, "open_now": False, "cheap": cheap}
    if cheap:
        return {"area": area, "open_now": False, "cheap": False}
    bigger = WIDER.get(area.lower())
    if not bigger or bigger.lower() == area.lower():
        return None
    return {"area": bigger, "open_now": False, "cheap": False}


def clean(value: Any) -> str:
    """One field from outside, made safe to print. Never adds a character.

    Control characters go, dashes become the word, whitespace collapses, and
    the result is capped. This is the only way a payload string becomes text a
    patient reads.
    """
    text = str(value or "")
    for dash in _DASHES:
        text = text.replace(dash, " to ")
    text = "".join(ch if ch.isprintable() else " " for ch in text)
    text = " ".join(text.split())
    return text[:MAX_FIELD].strip()


def safe(name: str, address: str) -> bool:
    """False when a listing reads like a dose. Then it is simply not sent.

    A display name is typed by whoever owns the listing, so it is untrusted
    text on its way to a patient. Sanad never puts a dose in front of anybody
    that the doctor did not write, and that has to be true of text this system
    only copied.
    """
    return not DOSE_LIKE.search(f"{name} {address}")


def link_for(place_id: str) -> str:
    """The Maps link, built here from the id and never taken from the payload."""
    clean_id = re.sub(r"[^A-Za-z0-9_\-]", "", str(place_id or ""))
    if not clean_id:
        return ""
    return f"https://www.google.com/maps/place/?q=place_id:{clean_id}"


def _today_hours(rows: Any, now: Optional[Any] = None) -> str:
    """That listing's own line for today, out of the seven the API returns.

    `weekdayDescriptions` is Monday first, which is what `weekday()` counts, and
    Cairo's day is the day the patient is standing in.
    """
    if not isinstance(rows, list) or not rows:
        return ""
    import datetime as dt

    when = now or dt.datetime.now(dt.timezone.utc)
    index = when.astimezone(timing.CAIRO).weekday()
    if index >= len(rows):
        return ""
    return clean(rows[index])


def parse(payload: Any, query: str = "") -> Search:
    """A Text Search response -> places. Anything unreadable is no place.

    The payload arrived over a network from outside this system, so every step
    of this reads defensively: a list where a dict belongs, a missing name, a
    row whose name reads like a dose. None of those may become a place a
    patient is sent to, and none of them may raise either.
    """
    rows = (payload or {}).get("places") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        return Search(query=query, error="the search answered with no places")
    out: list[Place] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        name = clean((row.get("displayName") or {}).get("text")
                     if isinstance(row.get("displayName"), dict) else "")
        if not name:
            continue
        address = clean(row.get("formattedAddress"))
        if not safe(name, address):
            log.warning("a places result was refused: its text reads like a dose")
            continue
        current = row.get("currentOpeningHours")
        open_now = (current.get("openNow") if isinstance(current, dict) else None)
        regular = row.get("regularOpeningHours")
        hours = _today_hours(regular.get("weekdayDescriptions")
                             if isinstance(regular, dict) else None)
        out.append(Place(
            name=name, address=address,
            open_now=open_now if isinstance(open_now, bool) else None,
            hours=hours, link=link_for(row.get("id")),
        ))
        if len(out) >= LIMIT:
            break
    return Search(query=query, places=tuple(out))


async def search(kind: str, area: str, *, open_now: bool = False,
                 cheap: bool = False) -> Search:
    """Places of one kind near one area. Never raises, never invents a place.

    The one call out of this file. `httpx` is imported here rather than at
    module scope so that the tables, the cleaning and the parsing above run
    with nothing installed, which is how the whole of this file is tested.
    """
    query = query_for(kind, area, cheap)
    if not query:
        return Search(error=f"{kind or 'that'} is not a kind Sanad searches for")
    key = os.environ.get("MAPS_API_KEY", "").strip()
    if not key:
        return Search(query=query, error=NO_KEY)
    if _hermetic():
        return Search(query=query, error=NO_NETWORK)

    import httpx

    body: dict[str, Any] = {"textQuery": query, "maxResultCount": LIMIT,
                            "languageCode": "en"}
    if open_now:
        body["openNow"] = True
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as client:
            answer = await client.post(
                ENDPOINT, json=body,
                headers={"Content-Type": "application/json",
                         "X-Goog-Api-Key": key,
                         "X-Goog-FieldMask": FIELD_MASK},
            )
        if answer.status_code != 200:
            return Search(query=query,
                          error=f"the search answered {answer.status_code}")
        return parse(answer.json(), query)
    except Exception as exc:  # noqa: BLE001 - every failure is the same answer
        log.warning("the places search failed", exc_info=True)
        return Search(query=query,
                      error=f"the search could not be reached: "
                            f"{' '.join(str(exc).split())[:80]}")


# --------------------------------------------------------------------------- #
# The fake, for tests
# --------------------------------------------------------------------------- #
@dataclass
class Fake:
    """A stand-in for `search`, so every route above is tested with no network.

    It records what it was asked for, which is how the routing table is tested:
    the assertion that a cost barrier searches for a government laboratory is
    an assertion about `calls`, not about a sentence in a docstring.
    """

    answers: list[Search] = field(default_factory=list)
    calls: list[dict[str, Any]] = field(default_factory=list)

    async def __call__(self, kind: str, area: str, *, open_now: bool = False,
                       cheap: bool = False) -> Search:
        self.calls.append({"kind": kind, "area": area, "open_now": open_now,
                           "cheap": cheap})
        if not self.answers:
            return Search(query=query_for(kind, area, cheap))
        return self.answers.pop(0)


def fake(*answers: Search) -> Fake:
    """`places.search` replaced by these answers, in order."""
    return Fake(answers=list(answers))


def found(*names: str, open_now: Optional[bool] = True, area: str = "Nasr City",
          kind: str = "lab", cheap: bool = False) -> Search:
    """One search that found these places. For tests and for nothing else."""
    return Search(
        query=query_for(kind, area, cheap),
        places=tuple(Place(name=name, address=f"{i} Some Street, {area}",
                           open_now=open_now, hours="Monday: 8 AM to 10 PM",
                           link=link_for(f"place{i}"))
                     for i, name in enumerate(names, start=1)),
    )
