"""Restrictions are versioned and preserve every existing domain row."""
import asyncio
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import admin_access
import user_access
import storage
import provider_registry as registry
import render_test_backup as backups


class UserAccessTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)/'isolated.sqlite'
        p = patch.object(storage,'DB_PATH',str(self.path))
        p.start();self.addCleanup(p.stop)
        with sqlite3.connect(self.path) as db:
            db.execute('CREATE TABLE leads(id INTEGER PRIMARY KEY, details TEXT)')
            db.execute("INSERT INTO leads VALUES (11,'Сохранить')")
        await admin_access.init_schema()
        await user_access.init_schema()
        await registry.init_schema(str(self.path))
        await registry.register_provider(30,'Исполнитель','+79991234567',True,str(self.path))
        await registry.set_notifications(30,True,str(self.path))
        await registry.save_alert_preferences(30,['автокран'],['Москва'],0,str(self.path))

    async def change(self,scopes,request='one',revision=0,actor=1,user=30):
        return await user_access.change(actor,user,scopes,'Тестовая причина',request,
             expected_revision=revision,owner_id=1,legacy_admins={1,2})

    async def test_owner_only_confirmation_protects_owner_and_staff(self):
        for actor,user,error in ((2,30,PermissionError),(1,1,ValueError),(1,2,ValueError)):
            with self.assertRaises(error):
                await self.change(['full'],actor=actor,user=user)
        self.assertEqual((await user_access.snapshot(30))['revision'],0)

    async def test_concurrent_change_and_idempotent_repeat_cannot_override_newer_access(self):
        result = await asyncio.gather(self.change(['requests'],'first'),self.change(['offers'],'second'),return_exceptions=True)
        self.assertEqual(sum(r is True for r in result),1)
        self.assertEqual(sum(isinstance(r,user_access.AccessConflict) for r in result),1)
        saved = await user_access.snapshot(30)
        winner = 'first' if saved['scopes']==['requests'] else 'second'
        await self.change([],request='clear',revision=saved['revision'])
        self.assertFalse(await self.change(saved['scopes'],request=winner))
        self.assertEqual((await user_access.snapshot(30))['scopes'],[])
        self.assertTrue(await user_access.allowed(30,'offers'))

    async def test_notifications_and_offer_restrictions_are_independent(self):
        async def matches(notifications=True):
            return await registry.matching_providers('автокран','Москва','СЗАО',str(self.path),notifications=notifications)
        self.assertEqual(await matches(),[30])
        await self.change(['requests','notifications'])
        self.assertEqual(await matches(),[])
        self.assertEqual(await matches(False),[30])
        saved=await user_access.snapshot(30)
        await self.change(['offers'],request='offers',revision=saved['revision'])
        self.assertEqual(await matches(),[30])
        self.assertEqual(await matches(False),[])
        saved=await user_access.snapshot(30)
        await self.change(['full'],request='full',revision=saved['revision'])
        self.assertEqual(await matches(),[])
        self.assertFalse(await user_access.allowed(30,'requests'))
        with sqlite3.connect(self.path) as db:
            self.assertEqual(db.execute('SELECT details FROM leads').fetchone(),('Сохранить',))
            self.assertEqual(db.execute('SELECT blocked FROM provider_profiles').fetchone(),(0,))

    async def test_snapshot_restore_and_repeat_initialization_preserve_restrictions(self):
        await self.change(['offers'])
        original=await user_access.snapshot(30)
        snap,digest=backups.snapshot(self.path)
        candidate=backups.restore_candidate(snap,digest)
        for _ in range(2):
            await user_access.init_schema(str(candidate))
            self.assertEqual(await user_access.snapshot(30,str(candidate)),original)
            self.assertGreater(backups.verify_preserved_rows(snap,candidate),4)
