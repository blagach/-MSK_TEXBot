"""Approved illustrations reach each role guide; no Telegram network."""
import hashlib
import json
import struct
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

import support_ui
from support_content import GUIDES


class ApprovedGuideTests(unittest.IsolatedAsyncioTestCase):
    async def test_all_twelve_approved_pages_are_sent_as_role_illustrations(self):
        manifest=json.loads((support_ui.ASSETS/'manifest.json').read_text())
        self.assertEqual(manifest['source_sha256'],'7543cd95cf64888648131ea2485757317f92a3395919917b5547d2fea85be7e6')
        self.assertEqual(len(manifest['pages']),12)
        for role in ('customer','provider'):
            self.assertEqual(len(GUIDES[role]),6)
            for index in range(6):
                with self.subTest(role=role,index=index):
                    message=type('Message',(),{'answer_photo':AsyncMock(),'answer':AsyncMock()})()
                    await support_ui.show_guide(message,role,index)
                    message.answer_photo.assert_awaited_once()
                    message.answer.assert_not_awaited()
                    photo=message.answer_photo.call_args.args[0]
                    path=Path(photo.path)
                    self.assertEqual(path,support_ui.ASSETS/f'{role}-{index+1}.png')
                    entry=next(r for r in manifest['pages'] if r['file']==path.name)
                    raw=path.read_bytes()
                    self.assertEqual(hashlib.sha256(raw).hexdigest(),entry['sha256'])
                    self.assertEqual(raw[:8],b'\x89PNG\r\n\x1a\n')
                    self.assertEqual(struct.unpack('>II',raw[16:24]),(entry['width'],entry['height']))
                    self.assertIn(f'{index+1}/6',message.answer_photo.call_args.kwargs['caption'])
