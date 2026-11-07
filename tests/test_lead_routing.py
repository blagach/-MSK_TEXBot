import os
import tempfile
import unittest
import lead_routing

class LeadRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.db=os.path.join(self.tmp.name,"test.db")
        await lead_routing.init_schema(self.db)
    async def asyncTearDown(self):
        self.tmp.cleanup()
    async def test_roundtrip(self):
        await lead_routing.save(7,"мини-погрузчик","Московская область","Химки",self.db)
        self.assertEqual(await lead_routing.get(7,self.db),{"category":"мини-погрузчик","region":"Московская область","area":"Химки"})
    async def test_missing_is_not_guessed(self):
        self.assertIsNone(await lead_routing.get(999,self.db))
    async def test_invalid_input_rejected(self):
        with self.assertRaises(ValueError):
            await lead_routing.save(8,"неизвестная","Московская область","Химки",self.db)
        with self.assertRaises(ValueError):
            await lead_routing.save(8,"мини-погрузчик","Московская область","",self.db)
    async def test_update_keeps_single_record(self):
        await lead_routing.save(7,"каток","Москва","СЗАО",self.db)
        await lead_routing.save(7,"каток","Москва","САО",self.db)
        self.assertEqual((await lead_routing.get(7,self.db))["area"],"САО")
