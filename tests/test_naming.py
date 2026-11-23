"""Tests for ticket-first naming, validation, and PR selector parsing."""

from __future__ import annotations

import unittest

from cubrid_worktree.errors import CliError
from cubrid_worktree.naming import (
    choose_review_target,
    extract_ticket_from_title,
    normalize_ticket,
    parse_pr_selector,
    parse_target,
)


class NamingTest(unittest.TestCase):
    def test_primary_ticket_normalization(self) -> None:
        parsed = parse_target("cbrd-28099")
        self.assertEqual(parsed.name, "CBRD-28099")
        self.assertEqual(parsed.ticket, "CBRD-28099")
        self.assertIsNone(parsed.summary)
        self.assertEqual(parsed.space_label, "CBRD-28099")
        self.assertIsNone(parsed.tab_label)

    def test_derived_or_non_pr_summary_forms(self) -> None:
        direct = parse_target("cbrd-28099-bench")
        via_flag = parse_target("CBRD-28099", summary="bench")
        self.assertEqual(direct, via_flag)
        self.assertEqual(direct.name, "CBRD-28099-bench")
        self.assertEqual(direct.ticket, "CBRD-28099")
        self.assertEqual(direct.summary, "bench")
        self.assertEqual(direct.space_label, "CBRD-28099")
        self.assertEqual(direct.tab_label, "bench")

        backport = parse_target("CBRD-28065-11.4")
        self.assertEqual(backport.name, "CBRD-28065-11.4")
        self.assertEqual(backport.tab_label, "11.4")

        numbered_summary = parse_target("CBRD-28099-test-2")
        self.assertEqual(numbered_summary.name, "CBRD-28099-test-2")
        self.assertEqual(numbered_summary.ticket, "CBRD-28099")
        self.assertEqual(numbered_summary.tab_label, "test-2")

        pr_fallback = parse_target("CBRD-28099-pr-7887")
        self.assertEqual(pr_fallback.name, "CBRD-28099-pr-7887")
        self.assertEqual(pr_fallback.ticket, "CBRD-28099")
        self.assertEqual(pr_fallback.tab_label, "pr-7887")

    def test_rejects_slashes_and_multiple_tickets(self) -> None:
        for invalid in ("CBRD-28099/bench", "CBRD-28099-CBRD-28100", "../escape", "-bad", "bad."):
            with self.subTest(invalid=invalid):
                with self.assertRaises(CliError) as ctx:
                    parse_target(invalid)
                self.assertEqual(ctx.exception.exit_code, 2)

        with self.assertRaises(CliError) as ctx:
            parse_target("CBRD-28099", summary="APIS-123")
        self.assertEqual(ctx.exception.code, "MULTIPLE_TICKETS")
        self.assertEqual(ctx.exception.exit_code, 2)

        with self.assertRaises(CliError) as ctx:
            parse_target("CBRD-28099-bench", summary="repro")
        self.assertEqual(ctx.exception.code, "CONFLICTING_SUMMARY")
        self.assertEqual(ctx.exception.exit_code, 2)

    def test_pr_selector_accepts_number_and_url(self) -> None:
        self.assertEqual(
            parse_pr_selector("7887"),
            (7887, "https://github.com/CUBRID/cubrid/pull/7887"),
        )
        self.assertEqual(
            parse_pr_selector("https://github.com/cubrid/cubrid/pull/7887/"),
            (7887, "https://github.com/CUBRID/cubrid/pull/7887"),
        )
        for bad in ("0", "-1", "https://github.com/other/repo/pull/7887", "not-a-pr"):
            with self.subTest(bad=bad):
                with self.assertRaises(CliError) as ctx:
                    parse_pr_selector(bad)
                self.assertEqual(ctx.exception.exit_code, 2)

    def test_review_target_selection_and_fallback(self) -> None:
        self.assertEqual(extract_ticket_from_title("[APIS-1087] [CBRD-27325] Fix"), "APIS-1087")
        self.assertIsNone(extract_ticket_from_title("Fix crash without ticket"))

        unoccupied = choose_review_target(7887, "[cbrd-27325] Fix heap", lambda _: False)
        self.assertEqual(unoccupied.name, "CBRD-27325")
        self.assertEqual(unoccupied.space_label, "CBRD-27325")
        self.assertIsNone(unoccupied.tab_label)

        occupied = choose_review_target(7887, "[CBRD-27325] Fix heap", lambda name: name == "CBRD-27325")
        self.assertEqual(occupied.name, "CBRD-27325-pr-7887")
        self.assertEqual(occupied.space_label, "CBRD-27325")
        self.assertEqual(occupied.tab_label, "pr-7887")

        unticketed = choose_review_target(8123, "Cleanup docs", lambda _: False)
        self.assertEqual(unticketed.name, "PR-8123")
        self.assertIsNone(unticketed.ticket)
        self.assertEqual(unticketed.space_label, "PR-8123")

    def test_normalize_ticket(self) -> None:
        self.assertEqual(normalize_ticket("cbrd-28099"), "CBRD-28099")
        with self.assertRaises(CliError):
            normalize_ticket("CBRD-28099-bench")
        with self.assertRaises(CliError):
            normalize_ticket("PR-12")


if __name__ == "__main__":
    unittest.main()
