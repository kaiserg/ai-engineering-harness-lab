import math
import unittest
from unittest.mock import patch
from harness.contracts import ContractError
from harness.knowledge import embed, split, validate
from harness.chroma_vectors import ChromaVectors


class KnowledgeUnitTests(unittest.TestCase):
    def test_embedding_is_normalized_deterministic_and_accent_insensitive(self):
        a=embed('rotación clave clave')
        self.assertEqual(a,embed('ROTACION clave clave'))
        self.assertEqual(len(a),256)
        self.assertAlmostEqual(sum(v*v for v in a),1)
        self.assertGreater(sum(x*y for x,y in zip(a,embed('clave'))),0)

    def test_empty_vector_and_unsafe_documents_are_rejected(self):
        for text in ('','😀'):
            with self.assertRaises(ContractError):embed(text)
        for filename in ('../secret.md','/tmp/key.txt','.env','a.pdf','a\\b.md'):
            with self.assertRaises(ContractError):validate(filename,'valid',[])
        validate('guía.md','Texto de ejemplo',['reader-demo'])
        with self.assertRaises(ContractError):validate('ok.md','a\x00b',[])

    def test_chunks_are_bounded_and_keep_overlap(self):
        text='a'*1700
        chunks=split(text)
        self.assertEqual([len(c) for c in chunks],[800,800,300])
        self.assertEqual(chunks[0][-100:],chunks[1][:100])

    def test_empty_acl_skips_chroma_and_malformed_results_fail_closed(self):
        vectors=ChromaVectors()
        with patch.object(vectors,'request') as request:
            self.assertEqual(vectors.query('anything',[],[],3),[])
            request.assert_not_called()
        for result in ({'ids':[['x']],'distances':[[math.nan]]},{'ids':[['x']],'distances':[[]]},{}):
            with patch.object(vectors,'collection',return_value='id'),patch.object(vectors,'request',return_value=result):
                with self.assertRaises(ContractError):vectors.query('name',[1],['doc'],3)
