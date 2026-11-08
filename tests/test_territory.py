"""Coverage tests: no Telegram API calls or production database."""
import os
import tempfile
import unittest
import provider_registry as registry

class TerritoryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.db=os.path.join(self.temp.name,"test.db")
        await registry.init_schema(self.db)
        for user in (101,102,103):
            await registry.register_provider(user,"Исполнитель","+79991234567",True,self.db)
        await registry.add_equipment(101,"каток","Каток","Москва","весь регион",self.db)
        await registry.add_equipment(102,"каток","Каток","Московская область","Химки",self.db)
        await registry.add_equipment(103,"каток","Каток",registry.BOTH_REGIONS,"весь регион",self.db)

    async def asyncTearDown(self):
        self.temp.cleanup()

    async def test_both_regions_match_moscow(self):
        self.assertEqual(await registry.matching_providers("каток","Москва","СЗАО",self.db),[101,103])

    async def test_both_regions_match_oblast(self):
        self.assertEqual(await registry.matching_providers("каток","Московская область","Химки",self.db),[102,103])

    async def test_both_regions_do_not_match_other_category(self):
        self.assertEqual(await registry.matching_providers("манипулятор","Москва","СЗАО",self.db),[])

    async def test_opt_out_applies_to_both_regions(self):
        await registry.set_notifications(103,False,self.db)
        self.assertEqual(await registry.matching_providers("каток","Москва","СЗАО",self.db),[101])

    async def test_block_applies_to_both_regions(self):
        await registry.set_blocked(103,True,admin_id=9,admin_ids={9},db_path=self.db)
        self.assertEqual(await registry.matching_providers("каток","Московская область","Химки",self.db),[102])
