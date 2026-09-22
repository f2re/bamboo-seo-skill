from __future__ import annotations

import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class DomainModelTests(unittest.TestCase):
    def test_domain_dictionary_shape_and_unique_ids(self):
        data=json.loads((ROOT/'docs/domain.json').read_text(encoding='utf-8'))
        self.assertEqual(data['schema_version'],1)
        ids=[x['id'] for x in data['terms']]
        self.assertEqual(len(ids),len(set(ids)))
        self.assertIn('yixing',ids)
        self.assertIn('kurinuki',ids)
        self.assertIn('chawan',ids)
        for term in data['terms']:
            self.assertTrue(term['preferred_ru'])
            self.assertTrue(term['aliases'])
            self.assertTrue(term['kind'])
            self.assertTrue(term['definition'])
            self.assertIsInstance(term.get('do_not_infer',[]),list)

    def test_yixing_rules_separate_technique_and_origin(self):
        data=json.loads((ROOT/'docs/domain.json').read_text(encoding='utf-8'))
        terms={x['id']:x for x in data['terms']}
        self.assertIn('clay_origin',terms['yixing']['do_not_infer'])
        self.assertTrue(terms['yixing-clay']['requires_evidence'])
        self.assertIn('guaranteed_taste_improvement',terms['yixing-clay']['do_not_infer'])


if __name__=='__main__':
    unittest.main()
