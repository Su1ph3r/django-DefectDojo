"""
Unit tests for the self-referential ``duplicate_finding`` FK in the chunked bulk finding delete.

Callers resolve inbound ``duplicate_finding`` references once, up front, before handing
the queryset to the chunked delete -- but the findings themselves are deleted in
per-chunk transactions. A reference written after that one-shot pass survives into its
chunk's COMMIT, where the self-FK rejects it. This is the same shape as the M2M
through-table window covered by ``test_bulk_delete_findings_m2m``, reached through the
duplicate self-FK instead.
"""

import logging

from django.db import connection
from django.utils import timezone

from dojo import utils_cascade_delete
from dojo.finding.helper import bulk_delete_findings
from dojo.models import (
    Engagement,
    Finding,
    Product,
    Product_Type,
    Test,
    Test_Type,
    User,
    UserContactInfo,
)

from .dojo_test_case import DojoTestCase

logger = logging.getLogger(__name__)


# Regression: deleting excess duplicates aborted at COMMIT with
# "update or delete on table dojo_finding violates foreign key constraint
# dojo_finding_duplicate_finding_id_... Key (id)=(N) is still referenced from
# table dojo_finding", because a reference written after the caller's up-front
# pass was never resolved.
class TestBulkDeleteFindingsDuplicateFK(DojoTestCase):

    """No surviving finding may still point at a finding the delete removed."""

    def setUp(self):
        super().setUp()
        self.testuser = User.objects.create(
            username="bulk_delete_dupe_fk_user",
            is_staff=True,
            is_superuser=True,
        )
        UserContactInfo.objects.create(user=self.testuser, block_execution=True)
        self.system_settings(enable_deduplication=False)
        self.system_settings(enable_product_grade=False)

        self.product_type = Product_Type.objects.create(name="Bulk Delete Dupe FK PT")
        self.product = Product.objects.create(
            name="Bulk Delete Dupe FK Product",
            description="Test",
            prod_type=self.product_type,
        )
        self.test_type = Test_Type.objects.get_or_create(name="Manual Test")[0]
        self.engagement = Engagement.objects.create(
            name="Bulk Delete Dupe FK Engagement",
            product=self.product,
            target_start=timezone.now(),
            target_end=timezone.now(),
        )
        self.test = Test.objects.create(
            engagement=self.engagement,
            test_type=self.test_type,
            target_start=timezone.now(),
            target_end=timezone.now(),
        )

    def _create_finding(self, title, duplicate_of=None):
        return Finding.objects.create(
            test=self.test,
            title=title,
            severity="High",
            description="Test",
            mitigation="Test",
            impact="Test",
            reporter=self.testuser,
            duplicate=duplicate_of is not None,
            duplicate_finding=duplicate_of,
        )

    def _delete_with_reference_written_mid_delete(self, doomed, write_reference):
        """
        Delete ``doomed`` one finding per chunk, writing a duplicate reference partway through.

        ``write_reference`` is invoked from inside the delete, after the first chunk has
        committed and before the remaining chunks are reached. That is the interleaving a
        concurrent import produces in production: dedupe points a finding at an original
        that the running delete has already selected but not yet removed.
        """
        # The delete imports execute_delete_sql at call time, so the patch has to land
        # on the defining module rather than on dojo.finding.helper.
        real_execute_delete_sql = utils_cascade_delete.execute_delete_sql
        state = {"calls": 0}

        def execute_delete_sql_with_concurrent_write(queryset, *args, **kwargs):
            result = real_execute_delete_sql(queryset, *args, **kwargs)
            state["calls"] += 1
            if state["calls"] == 1:
                write_reference()
            return result

        utils_cascade_delete.execute_delete_sql = execute_delete_sql_with_concurrent_write
        try:
            bulk_delete_findings(
                Finding.objects.filter(id__in=[finding.id for finding in doomed]),
                chunk_size=1,
                order_desc=True,
            )
        finally:
            utils_cascade_delete.execute_delete_sql = real_execute_delete_sql
        return state["calls"]

    def test_reference_written_mid_delete_is_repointed_to_surviving_original(self):
        """
        A duplicate reference that lands mid-delete must be resolved in its chunk's transaction.

        Django declares its foreign keys DEFERRABLE INITIALLY DEFERRED, so in production
        the leftover reference is only rejected at the chunk's COMMIT -- far from the code
        that created it, surfacing as an opaque constraint error that aborts the delete
        task. Inside a TestCase transaction the same condition is what
        check_constraints() reports.
        """
        original = self._create_finding("Dupe FK original")
        doomed_first = self._create_finding("Dupe FK doomed A", duplicate_of=original)
        doomed_second = self._create_finding("Dupe FK doomed B", duplicate_of=original)
        survivor = self._create_finding("Dupe FK survivor")

        # order_desc deletes the higher id first, so doomed_first is still present when
        # the reference is written and is removed by a later chunk.
        def point_survivor_at_doomed_first():
            Finding.objects.filter(id=survivor.id).update(
                duplicate=True,
                duplicate_finding_id=doomed_first.id,
            )

        self._delete_with_reference_written_mid_delete(
            [doomed_first, doomed_second],
            point_survivor_at_doomed_first,
        )

        self.assertFalse(
            Finding.objects.filter(id__in=[doomed_first.id, doomed_second.id]).exists(),
            "Both excess duplicates should have been deleted.",
        )
        survivor.refresh_from_db()
        self.assertEqual(
            survivor.duplicate_finding_id, original.id,
            msg=(
                "the survivor should have been re-pointed at the surviving original, "
                f"persisted duplicate_finding_id={survivor.duplicate_finding_id}"
            ),
        )
        self.assertTrue(
            survivor.duplicate,
            "re-pointing at a surviving original keeps the finding a duplicate.",
        )
        # The check Postgres runs at COMMIT, where a leftover reference is the
        # IntegrityError that fails the delete in production.
        connection.check_constraints()

    def test_reference_written_mid_delete_is_promoted_when_no_original_survives(self):
        """Same window, but the doomed finding has no surviving original to inherit."""
        doomed_first = self._create_finding("Dupe FK orphan doomed A")
        doomed_second = self._create_finding("Dupe FK orphan doomed B")
        survivor = self._create_finding("Dupe FK orphan survivor")

        def point_survivor_at_doomed_first():
            Finding.objects.filter(id=survivor.id).update(
                duplicate=True,
                duplicate_finding_id=doomed_first.id,
            )

        self._delete_with_reference_written_mid_delete(
            [doomed_first, doomed_second],
            point_survivor_at_doomed_first,
        )

        self.assertFalse(
            Finding.objects.filter(id__in=[doomed_first.id, doomed_second.id]).exists(),
            "Both findings should have been deleted.",
        )
        survivor.refresh_from_db()
        self.assertIsNone(
            survivor.duplicate_finding_id,
            msg=(
                "with no surviving original the survivor should be promoted, "
                f"persisted duplicate_finding_id={survivor.duplicate_finding_id}"
            ),
        )
        self.assertFalse(
            survivor.duplicate,
            "a promoted finding is no longer flagged as a duplicate.",
        )
        connection.check_constraints()

    def test_reference_written_mid_delete_into_the_delete_set_is_left_alone(self):
        """
        Only findings that outlive the delete are resolved.

        A reference from one doomed finding to another goes away with the row that holds
        it, so re-pointing it would be pointless churn.
        """
        original = self._create_finding("Dupe FK inside original")
        doomed_first = self._create_finding("Dupe FK inside doomed A", duplicate_of=original)
        doomed_second = self._create_finding("Dupe FK inside doomed B", duplicate_of=original)

        def point_doomed_second_at_doomed_first():
            # doomed_second is deleted in the first chunk (highest id), so this write
            # never actually lands; the callback exists to keep the harness identical.
            Finding.objects.filter(id=doomed_second.id).update(
                duplicate_finding_id=doomed_first.id,
            )

        self._delete_with_reference_written_mid_delete(
            [doomed_first, doomed_second],
            point_doomed_second_at_doomed_first,
        )

        self.assertFalse(
            Finding.objects.filter(id__in=[doomed_first.id, doomed_second.id]).exists(),
            "Both excess duplicates should have been deleted.",
        )
        original.refresh_from_db()
        self.assertIsNone(
            original.duplicate_finding_id,
            "the surviving original must not be touched by the delete.",
        )
        connection.check_constraints()

    def test_existing_duplicate_references_delete_cleanly(self):
        """Control: with no concurrent write the ordinary path is unchanged."""
        original = self._create_finding("Dupe FK control original")
        doomed_first = self._create_finding("Dupe FK control doomed A", duplicate_of=original)
        doomed_second = self._create_finding("Dupe FK control doomed B", duplicate_of=original)
        survivor = self._create_finding("Dupe FK control survivor", duplicate_of=original)

        bulk_delete_findings(
            Finding.objects.filter(id__in=[doomed_first.id, doomed_second.id]),
            chunk_size=1,
            order_desc=True,
        )

        self.assertFalse(
            Finding.objects.filter(id__in=[doomed_first.id, doomed_second.id]).exists(),
            "Both excess duplicates should have been deleted.",
        )
        survivor.refresh_from_db()
        self.assertEqual(
            survivor.duplicate_finding_id, original.id,
            msg=(
                "an untouched duplicate keeps pointing at its original, "
                f"persisted duplicate_finding_id={survivor.duplicate_finding_id}"
            ),
        )
        self.assertTrue(survivor.duplicate, "an untouched duplicate stays a duplicate.")
        connection.check_constraints()
