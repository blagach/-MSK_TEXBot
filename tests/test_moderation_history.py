import os
import tempfile
import unittest
import moderation_history as history

class ModerationHistoryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.db=os.path.join(self.temp.name,"test.sqlite")
        await history.init_schema(self.db)
    async def asyncTearDown(self):
        self.temp.cleanup()
    async def test_new_customer_not_approved(self):
        self.assertFalse(await history.approved_before(11,self.db))
    async def test_approval_persists(self):
        await history.record_approval(11,21,self.db)
        self.assertTrue(await history.approved_before(11,self.db))
        await history.record_approval(11,22,self.db)
        self.assertTrue(await history.approved_before(11,self.db))
    async def test_publication_claim_only_once(self):
        self.assertTrue(await history.claim_publication(21,self.db))
        self.assertFalse(await history.claim_publication(21,self.db))
    async def test_unknown_outcome_does_not_allow_retry(self):
        await history.claim_publication(21,self.db)
        await history.mark_outcome(21,"unknown",self.db)
        self.assertFalse(await history.claim_publication(21,self.db))
    async def test_invalid_outcome_rejected(self):
        with self.assertRaises(ValueError):
            await history.mark_outcome(21,"retry",self.db)
