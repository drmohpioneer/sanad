"""core/places.py: the one thing Sanad reads from outside the doctor's records.

S19. Everything here runs with nothing installed, no key and no network. That
is the point of the file being separate from the search itself: the query
table, the cleaning, the parsing, the widening and the fail-soft answers are
all pure functions, and the one coroutine that talks to Google is driven here
against a fake HTTP layer.

The rails that matter most, in the order a reader should care about them:

  it never fabricates a place        an unreadable payload is no places, not a
                                     guess, and never an exception
  it never quotes a price            Maps has none; `cheap` changes the words
                                     of the query and nothing else
  it fails soft, visibly             no key, a network error and a non-200 all
                                     come back as a `Search` with an `error`
                                     the doctor's hand-over card can print, and
                                     "could not look" never reads as "found
                                     nothing"
  a listing is untrusted text        it is cleaned, capped, de-dashed, and one
                                     that reads like a dose is dropped
"""

from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace

from core import places

APP_ROOT = Path(__file__).resolve().parents[1]


class TheQueryIsATable(unittest.TestCase):
    def test_each_kind_and_each_bias_has_its_own_words(self) -> None:
        self.assertEqual(places.query_for("lab", "Nasr City"),
                         "medical laboratory in Nasr City")
        self.assertEqual(places.query_for("lab", "Shubra", cheap=True),
                         "government hospital medical laboratory in Shubra")
        self.assertEqual(places.query_for("pharmacy", "Mansoura"),
                         "pharmacy in Mansoura")
        self.assertEqual(places.query_for("pharmacy", "Mansoura", cheap=True),
                         "government pharmacy in Mansoura")

    def test_a_kind_that_is_not_a_kind_produces_no_query_at_all(self) -> None:
        """"clinic" is the one a reader will look for: sending a patient to a
        different doctor is a referral and there is no tool for one here."""
        self.assertEqual(places.query_for("clinic", "Nasr City"), "")
        self.assertEqual(places.query_for("", "Nasr City"), "")
        self.assertNotIn("clinic", places.KINDS)

    def test_the_cheaper_query_never_claims_a_price(self) -> None:
        for (kind, cheap), shape in places.QUERIES.items():
            with self.subTest(kind=kind, cheap=cheap):
                self.assertFalse(any(ch.isdigit() for ch in shape))
                for word in ("price", "cheap", "egp", "cost"):
                    self.assertNotIn(word, shape.lower())


class APlaceIsDataFromOutside(unittest.TestCase):
    def test_whitespace_and_control_characters_are_dropped(self) -> None:
        self.assertEqual(places.clean("  Alfa\n\tLab  "), "Alfa Lab")

    def test_a_dash_becomes_the_word_sanad_would_have_written(self) -> None:
        self.assertEqual(places.clean("Monday: 8 AM – 10 PM"),
                         "Monday: 8 AM to 10 PM")

    def test_a_field_long_enough_to_hide_something_in_is_capped(self) -> None:
        self.assertEqual(len(places.clean("x" * 500)), places.MAX_FIELD)

    def test_a_listing_that_reads_like_a_dose_never_reaches_a_patient(self) -> None:
        for name in ("Take 4 mg Pharmacy", "El Dose Lab", "صيدلية جرعة"):
            with self.subTest(name=name):
                self.assertFalse(places.safe(name, ""))

    def test_an_ordinary_listing_is_not_refused_by_that_rail(self) -> None:
        """A rail that refuses real laboratories is a rail that hands the
        doctor a barrier nobody needed to see."""
        for name, address in (("Take Care Pharmacy", "24 Abbas El Akkad"),
                              ("Alfa Lab", "10 Street 9, Nasr City"),
                              ("El Borg Laboratories", "Mostafa El Nahas")):
            with self.subTest(name=name):
                self.assertTrue(places.safe(name, address))

    def test_the_maps_link_is_built_here_and_never_taken_from_the_payload(
            self) -> None:
        self.assertEqual(places.link_for("ChIJ_x-1"),
                         "https://www.google.com/maps/place/?q=place_id:ChIJ_x-1")
        self.assertEqual(places.link_for("javascript:alert(1)"),
                         "https://www.google.com/maps/place/?q=place_id:"
                         "javascriptalert1")
        self.assertEqual(places.link_for(""), "")
        self.assertEqual(places.link_for(None), "")


class ReadingWhatTheApiAnswered(unittest.TestCase):
    def payload(self, *rows) -> dict:
        return {"places": list(rows)}

    def row(self, name="Alfa Lab", address="10 Street 9, Nasr City",
            open_now=True):
        return {"id": "abc", "displayName": {"text": name},
                "formattedAddress": address,
                "currentOpeningHours": {"openNow": open_now},
                "regularOpeningHours": {"weekdayDescriptions": [
                    f"{day}: 8 AM – 10 PM" for day in
                    ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday",
                     "Saturday", "Sunday")]}}

    def test_one_row_becomes_one_place_with_its_own_printed_fields(self) -> None:
        found = places.parse(self.payload(self.row()), "medical laboratory")
        self.assertEqual(len(found), 1)
        place = found.places[0]
        self.assertEqual(place.name, "Alfa Lab")
        self.assertEqual(place.address, "10 Street 9, Nasr City")
        self.assertTrue(place.open_now)
        self.assertIn(" to ", place.hours)
        self.assertTrue(place.link.endswith("place_id:abc"))

    def test_never_more_than_the_limit_a_patient_can_act_on(self) -> None:
        found = places.parse(self.payload(*[self.row() for _ in range(9)]))
        self.assertEqual(len(found), places.LIMIT)

    def test_a_row_that_reads_like_a_dose_is_left_out_of_the_answer(self) -> None:
        found = places.parse(self.payload(self.row(name="Take 4 mg Lab"),
                                          self.row(name="Alfa Lab")))
        self.assertEqual([p.name for p in found.places], ["Alfa Lab"])

    def test_an_unreadable_payload_is_no_places_and_never_an_exception(self) -> None:
        for bad in ({}, {"places": "nope"}, None, {"places": [None, 3, {}]}):
            with self.subTest(bad=bad):
                found = places.parse(bad)
                self.assertEqual(len(found), 0)

    def test_a_listing_with_no_published_hours_is_not_called_closed(self) -> None:
        found = places.parse(self.payload(
            {"id": "x", "displayName": {"text": "Alfa Lab"}}))
        self.assertIsNone(found.places[0].open_now)
        self.assertEqual(found.places[0].hours, "")
        self.assertNotIn("closed", found.places[0].line())

    def test_the_block_a_patient_reads_is_numbered_and_carries_the_link(
            self) -> None:
        block = places.parse(self.payload(self.row(), self.row(name="Beta Lab"))
                             ).block()
        self.assertTrue(block.startswith("1. Alfa Lab, "))
        self.assertIn("2. Beta Lab", block)
        self.assertIn("open now", block)
        self.assertIn("https://www.google.com/maps/place/", block)


class TheSearchFailsSoft(unittest.IsolatedAsyncioTestCase):
    """No key is a supported state, not a broken one."""

    async def test_no_key_is_an_answer_and_not_an_exception(self) -> None:
        from unittest.mock import patch

        with patch.dict("os.environ", {}, clear=False) as env:
            import os
            os.environ.pop("MAPS_API_KEY", None)
            found = await places.search("lab", "Nasr City")
        self.assertTrue(found.unavailable)
        self.assertEqual(found.error, places.NO_KEY)
        self.assertEqual(len(found), 0)
        self.assertIn("MAPS_API_KEY", found.tried())

    async def test_a_kind_that_is_not_a_kind_never_reaches_the_network(
            self) -> None:
        found = await places.search("clinic", "Nasr City")
        self.assertTrue(found.unavailable)
        self.assertIn("not a kind", found.error)

    async def test_a_network_failure_is_the_same_kind_of_answer(self) -> None:
        from unittest.mock import patch

        import httpx

        class Explodes:
            def __init__(self, *a, **kw):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def post(self, *a, **kw):
                raise RuntimeError("the network is down")

        import os
        with patch.dict("os.environ", {"MAPS_API_KEY": "test-key"}), \
                patch.object(places, "_hermetic", lambda: False), \
                patch.object(httpx, "AsyncClient", Explodes):
            found = await places.search("lab", "Nasr City")
        self.assertTrue(found.unavailable)
        self.assertIn("could not be reached", found.error)

    async def test_a_non_200_says_which_one(self) -> None:
        from unittest.mock import patch

        import httpx

        class Refuses:
            def __init__(self, *a, **kw):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def post(self, *a, **kw):
                return SimpleNamespace(status_code=403, json=lambda: {})

        with patch.dict("os.environ", {"MAPS_API_KEY": "test-key"}), \
                patch.object(places, "_hermetic", lambda: False), \
                patch.object(httpx, "AsyncClient", Refuses):
            found = await places.search("lab", "Nasr City")
        self.assertIn("403", found.error)

    def test_a_search_never_starts_inside_the_hermetic_boundary(self) -> None:
        """The suite has no socket, and a search must not go looking for one.

        app/sanad_test_guard.py refuses every socket in this process and raises
        a BaseException doing it, which the `except Exception` at the bottom of
        `search` could not catch: an unmocked search would be a crash and not
        the soft failure every caller is written against. So the probe is read
        before the client is built, exactly as core/auditor.py reads it before
        a model call, and no network means the same answer no key means.
        """
        self.assertTrue(places._hermetic())

    async def test_the_probe_answers_before_a_client_is_ever_built(self) -> None:
        from unittest.mock import patch

        with patch.dict("os.environ", {"MAPS_API_KEY": "test-key"}):
            found = await places.search("lab", "Nasr City")
        self.assertTrue(found.unavailable)
        self.assertEqual(places.NO_NETWORK, found.error)
        self.assertEqual((), found.places)

    def test_the_key_is_mounted_by_the_deploy_script(self) -> None:
        """The Resolver's key reaches the service the way the bot token does.

        This test checks the deployment boundary directly rather than changing
        the frozen Gate 0B `/health` payload.
        """
        deploy = (APP_ROOT / "deploy.sh").read_text(encoding="utf-8")
        self.assertIn("MAPS_SECRET=sanad-maps-key", deploy)
        self.assertIn("MAPS_API_KEY=${MAPS_SECRET}:latest", deploy)

    def test_configured_is_the_env_var_and_not_a_constant(self) -> None:
        """A deployed revision with no key has to be visible as one."""
        import os
        from unittest.mock import patch

        with patch.dict("os.environ", {"MAPS_API_KEY": "test-key"}):
            self.assertTrue(places.configured())
        with patch.dict("os.environ", {"MAPS_API_KEY": "   "}):
            self.assertFalse(places.configured())
        with patch.dict("os.environ", {}, clear=False):
            os.environ.pop("MAPS_API_KEY", None)
            self.assertFalse(places.configured())


class TheOneCallOutOfThisFile(unittest.IsolatedAsyncioTestCase):
    """`search` end to end, against a fake HTTP layer. No network, no key.

    What is asserted here is the request as well as the answer: the endpoint,
    the key header, the field mask and the body are the contract with Google,
    and a field mask that stopped asking for opening hours would silently make
    every place Sanad sends read as having none.
    """

    def setUp(self) -> None:
        self.calls: list = []
        outer = self

        class Client:
            """One fake httpx.AsyncClient. It answers what the test set."""

            payload: dict = {"places": []}
            status: int = 200

            def __init__(self, *a, **kw):
                outer.calls.append({"init": kw})

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def post(self, url, json=None, headers=None):
                outer.calls.append({"url": url, "json": json,
                                    "headers": headers})
                return SimpleNamespace(status_code=Client.status,
                                       json=lambda: Client.payload)

        self.Client = Client

    async def run_search(self, payload=None, status=200, **kw):
        from unittest.mock import patch

        import httpx

        self.Client.payload = payload if payload is not None else {"places": []}
        self.Client.status = status
        with patch.dict("os.environ", {"MAPS_API_KEY": "test-key"}), \
                patch.object(places, "_hermetic", lambda: False), \
                patch.object(httpx, "AsyncClient", self.Client):
            return await places.search(kw.pop("kind", "lab"),
                                       kw.pop("area", "Nasr City"), **kw)

    def row(self, name="Alfa Lab", open_now=True, hours=True):
        row = {"id": "abc", "displayName": {"text": name},
               "formattedAddress": "10 Street 9, Nasr City"}
        if open_now is not None:
            row["currentOpeningHours"] = {"openNow": open_now}
        if hours:
            row["regularOpeningHours"] = {"weekdayDescriptions": [
                f"{day}: 8 AM to 10 PM" for day in
                ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday",
                 "Saturday", "Sunday")]}
        return row

    async def test_the_request_is_the_contract_with_google(self) -> None:
        await self.run_search(open_now=True)
        post = [c for c in self.calls if "url" in c][-1]
        self.assertEqual(post["url"], places.ENDPOINT)
        self.assertEqual(post["headers"]["X-Goog-Api-Key"], "test-key")
        self.assertEqual(post["headers"]["X-Goog-FieldMask"], places.FIELD_MASK)
        self.assertEqual(post["json"]["textQuery"],
                         "medical laboratory in Nasr City")
        self.assertEqual(post["json"]["maxResultCount"], places.LIMIT)
        self.assertTrue(post["json"]["openNow"])

    async def test_open_now_is_left_out_when_it_was_not_asked_for(self) -> None:
        await self.run_search(open_now=False)
        post = [c for c in self.calls if "url" in c][-1]
        self.assertNotIn("openNow", post["json"])

    async def test_the_cheaper_search_asks_for_the_public_sector(self) -> None:
        await self.run_search(cheap=True)
        post = [c for c in self.calls if "url" in c][-1]
        self.assertEqual(post["json"]["textQuery"],
                         "government hospital medical laboratory in Nasr City")

    async def test_a_good_answer_becomes_places_a_patient_can_use(self) -> None:
        found = await self.run_search({"places": [self.row(),
                                                  self.row("Beta Lab")]})
        self.assertFalse(found.unavailable)
        self.assertEqual([p.name for p in found.places],
                         ["Alfa Lab", "Beta Lab"])
        self.assertTrue(found.places[0].open_now)
        self.assertIn("8 AM to 10 PM", found.places[0].hours)
        self.assertIn("2 found", found.tried())

    async def test_a_row_with_no_hours_at_all_is_not_called_closed(self) -> None:
        found = await self.run_search(
            {"places": [self.row(open_now=None, hours=False)]})
        self.assertIsNone(found.places[0].open_now)
        self.assertNotIn("closed", found.block())

    async def test_an_empty_answer_is_zero_places_and_says_so(self) -> None:
        found = await self.run_search({"places": []})
        self.assertEqual(len(found), 0)
        self.assertFalse(found.unavailable)
        self.assertIn("nothing found", found.tried())

    async def test_a_payload_that_makes_no_sense_never_becomes_a_place(
            self) -> None:
        for payload in ({"error": {"code": 400}}, {"places": {"a": 1}},
                        {"places": [{"displayName": "not a dict"}]},
                        {"places": [{"id": "x"}]}):
            with self.subTest(payload=payload):
                found = await self.run_search(payload)
                self.assertEqual(len(found), 0)

    async def test_a_non_200_is_an_error_and_never_an_empty_result(self) -> None:
        """"Could not look" and "found nothing" are different sentences and the
        doctor's card has to be able to tell them apart."""
        found = await self.run_search({"places": [self.row()]}, status=500)
        self.assertTrue(found.unavailable)
        self.assertIn("500", found.error)
        self.assertEqual(len(found), 0)
        self.assertNotIn("nothing found", found.tried())


class TheSecondSearchWhenTheFirstFoundNothing(unittest.TestCase):
    """The adapting step, as the table that decides it. Pure."""

    def test_open_now_is_the_first_thing_relaxed(self) -> None:
        """A laboratory that is shut this evening is still one he can use."""
        self.assertEqual(
            places.widen("Nasr City", open_now=True, cheap=False),
            {"area": "Nasr City", "open_now": False, "cheap": False})

    def test_a_cheaper_search_keeps_its_bias_while_open_now_is_dropped(
            self) -> None:
        self.assertEqual(
            places.widen("Shubra", open_now=True, cheap=True),
            {"area": "Shubra", "open_now": False, "cheap": True})

    def test_the_public_sector_bias_is_relaxed_second(self) -> None:
        self.assertEqual(
            places.widen("Shubra", open_now=False, cheap=True),
            {"area": "Shubra", "open_now": False, "cheap": False})

    def test_the_area_is_widened_last_and_only_where_it_is_known(self) -> None:
        self.assertEqual(
            places.widen("Nasr City", open_now=False, cheap=False),
            {"area": "Cairo", "open_now": False, "cheap": False})
        self.assertEqual(
            places.widen("zagazig", open_now=False, cheap=False)["area"],
            "Sharqia")

    def test_an_area_sanad_cannot_widen_honestly_is_not_widened(self) -> None:
        """No gazetteer entry, no second search of somewhere invented."""
        for area in ("Minya", "Alexandria", "Aswan", "somewhere else", ""):
            with self.subTest(area=area):
                self.assertIsNone(
                    places.widen(area, open_now=False, cheap=False))

    def test_widening_never_changes_the_kind_of_place(self) -> None:
        wider = places.widen("Nasr City", open_now=True, cheap=False)
        self.assertNotIn("kind", wider)

    def test_the_sentence_for_an_area_with_nowhere_wider_is_a_real_sentence(
            self) -> None:
        self.assertEqual(places.NO_WIDER.format(area="Minya"),
                         "no wider area is known for Minya")


if __name__ == "__main__":
    unittest.main()
