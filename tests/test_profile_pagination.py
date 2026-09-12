from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "outputs/TikTokBatchMVP"))
from profile_pagination import ProfilePagination


def url(username="owner", cursor="0", sec_uid="sec-owner"):
    return ("https://www.tiktok.com/api/post/item_list/?aid=1988&"
            f"secUid={sec_uid}&cursor={cursor}&count=30")


def page(ids, owner="owner", cursor="30", more=True):
    return {"statusCode": 0, "hasMore": more, "cursor": cursor,
            "itemList": [{"id": str(ident), "author": {"uniqueId": owner}}
                         for ident in ids]}


class PaginationTests(unittest.TestCase):
    def test_does_not_reuse_signed_url_for_a_changed_cursor(self):
        p = ProfilePagination("owner")
        p.observe(url()+"&X-Bogus=test-signature", page([1],cursor="30"))
        self.assertEqual(p.next_cursor(), "30")
        self.assertIsNone(p.continuation_url())

    def test_repeated_ids_do_not_inflate_count(self):
        p = ProfilePagination("owner")
        self.assertTrue(p.observe(url(cursor="0"), page([1, 2], cursor="30")))
        self.assertTrue(p.observe(url(cursor="30"), page([2, 3], cursor="60")))
        self.assertEqual(set(p.items), {"1", "2", "3"})
        self.assertEqual(p.next_cursor(), "60")

    def test_unrelated_terminal_page_cannot_finish_target(self):
        p = ProfilePagination("owner")
        self.assertFalse(p.observe(url("other", sec_uid="sec-other"),
                                   page([], owner="other", more=False)))
        self.assertFalse(p.complete)
        self.assertTrue(p.observe(url(), page([1], more=False)))
        self.assertTrue(p.complete)

    def test_cursor_chain_builds_continuation_and_finishes(self):
        p = ProfilePagination("owner")
        p.observe(url(cursor="0"), page([1], cursor="30", more=True))
        self.assertIn("cursor=30", p.continuation_url())
        p.observe(url(cursor="30"), page([2], more=False))
        self.assertTrue(p.complete)
        self.assertIsNone(p.continuation_url())

    def test_first_error_time_is_sticky_until_success(self):
        p = ProfilePagination("owner")
        p.observe(url(), page([1], cursor="30"))
        with patch("profile_pagination.time.monotonic", side_effect=[10, 20]):
            p.observe(url(cursor="30"), None)
            first = p.error_at
            p.observe(url(cursor="30"), None)
        self.assertEqual(first, p.error_at)
        p.observe(url(cursor="30"), page([2], more=False))
        self.assertIsNone(p.error_at)

    def test_wrong_owner_does_not_rebind_or_finish(self):
        p = ProfilePagination("owner")
        p.observe(url(), page([1], cursor="30"))
        self.assertFalse(p.observe(url("other", cursor="0", sec_uid="sec-other"),
                                   page([9], owner="other", more=False)))
        self.assertFalse(p.complete)
        self.assertEqual(set(p.items), {"1"})

    def test_refusal_halts_continuation(self):
        p = ProfilePagination("owner")
        p.observe(url(), page([1], cursor="30"))
        p.observe(url(cursor="30"), {"statusCode": 10201, "itemList": []}, 200)
        self.assertTrue(p.refused)
        self.assertIsNone(p.continuation_url())


if __name__ == "__main__":
    unittest.main()
