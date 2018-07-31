# Copyright 2017 Neural Networks and Deep Learning lab, MIPT
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from collections import Counter
from typing import List, Any, Generator, Tuple, KeysView, ValuesView, Type, Dict, Optional

import scipy as sp
from scipy import sparse
import numpy as np
from sklearn.utils import murmurhash3_32

from deeppavlov.core.models.component import Component
from deeppavlov.core.models.serializable import Serializable
from deeppavlov.core.common.log import get_logger
from deeppavlov.core.common.registry import register

TOKENIZER = None
logger = get_logger(__name__)

Sparse = sp.sparse.csr_matrix


def hash_(token: str, hash_size: int) -> int:
    """Convert a token to a hash of given size.
    Args:
        token: a word
        hash_size: hash size

    Returns:
        int, hashed token

    """
    return murmurhash3_32(token, positive=True) % hash_size


@register('hashing_tfidf_vectorizer')
class HashingTfIdfVectorizer(Component, Serializable):
    """Create a tfidf matrix from collection of documents.

    Args:
        tokenizer: a tokenizer class
        hash_size: a hash size, power of two
        doc_index: a dictinary of document ids and their titles
        save_path: a path to .npz file where tfidf matrix is saved
        load_path: a path to .npz file where tfidf matrix is loaded from

    Attributes:
        hash_size: a hash size
        tokenizer: instance of a tokenizer class
        random: instance of ``Random`` initialized with a seed
        term_freqs: a dictionary with tfidf terms and their frequences
        doc_index: provided by a user ids or generated automatically ids
        rows: tfidf matrix rows corresponding to terms
        cols: tfidf matrix cols corresponding to docs
        data: tfidf matrix data corresponding to tfidf values

    """

    def __init__(self, tokenizer: Component, hash_size=2 ** 24, doc_index: Optional[dict] = None,
                 save_path: Optional[str] = None, load_path: Optional[str] = None, **kwargs):

        super().__init__(save_path=save_path, load_path=load_path, mode=kwargs.get('mode', 'infer'))

        self.hash_size = hash_size
        self.tokenizer = tokenizer
        self.term_freqs = None
        self.doc_index = doc_index

        global TOKENIZER
        TOKENIZER = self.tokenizer

        self.rows = []
        self.cols = []
        self.data = []

    def __call__(self, questions: List[str]) -> Sparse:
        """Transform input list of documents to a tfidf vectors.

        Args:
            questions: a list of input strings

        Return:
            transformed documents as a csr_matrix

        """

        sp_tfidfs = []

        for question in questions:
            ngrams = list(self.tokenizer([question]))
            hashes = [hash_(ngram, self.hash_size) for ngram in ngrams[0]]

            hashes_unique, q_hashes = np.unique(hashes, return_counts=True)
            tfs = np.log1p(q_hashes)

            # TODO revise policy if len(q_hashes) == 0

            if len(q_hashes) == 0:
                return sp.sparse.csr_matrix((1, self.hash_size))

            size = len(self.doc_index)
            Ns = self.term_freqs[hashes_unique]
            idfs = np.log((size - Ns + 0.5) / (Ns + 0.5))
            idfs[idfs < 0] = 0

            tfidf = np.multiply(tfs, idfs)

            indptr = np.array([0, len(hashes_unique)])
            sp_tfidf = sp.sparse.csr_matrix(
                (tfidf, hashes_unique, indptr), shape=(1, self.hash_size)
            )
            sp_tfidfs.append(sp_tfidf)

        transformed = sp.sparse.vstack(sp_tfidfs)
        return transformed

    def get_counts(self, docs: List[str], doc_ids: List[Any]) \
            -> Generator[Tuple[KeysView, ValuesView, List[int]], Any, None]:
        """Get term counts for a list of documents.

        Args:
            docs: a list of input documents
            doc_ids: a list of document ids corresponding to input documents

        Yields:
            a tuple of term hashes, count values and column ids

        Returns:
            None

        """
        logger.info("Tokenizing batch...")
        batch_ngrams = list(self.tokenizer(docs))
        logger.info("Counting hash...")
        doc_id = iter(doc_ids)
        for ngrams in batch_ngrams:
            counts = Counter([hash_(gram, self.hash_size) for gram in ngrams])
            hashes = counts.keys()
            values = counts.values()
            _id = self.doc_index[next(doc_id)]
            if values:
                col_id = [_id] * len(values)
            else:
                col_id = []
            yield hashes, values, col_id

    def get_count_matrix(self, row: List[int], col: List[int], data: List[int], size) \
            -> Sparse:
        """Get count matrix.

        Args:
            row: tfidf matrix rows corresponding to terms
            col:  tfidf matrix cols corresponding to docs
            data: tfidf matrix data corresponding to tfidf values
            size: self.doc_index size

        Returns:
            a count csr_matrix

        """
        count_matrix = sparse.csr_matrix((data, (row, col)), shape=(self.hash_size, size))
        count_matrix.sum_duplicates()
        return count_matrix

    @staticmethod
    def get_tfidf_matrix(count_matrix: sp.sparse.csr_matrix) -> Tuple[Sparse, np.array]:
        """Convert a count matrix into a tfidf matrix.

        Args:
            count_matrix: a count matrix

        Returns:
            a tuple of tfidf matrix and term frequences

        """

        binary = (count_matrix > 0).astype(int)
        term_freqs = np.array(binary.sum(1)).squeeze()
        idfs = np.log((count_matrix.shape[1] - term_freqs + 0.5) / (term_freqs + 0.5))
        idfs[idfs < 0] = 0
        idfs = sp.sparse.diags(idfs, 0)
        tfs = count_matrix.log1p()
        tfidfs = idfs.dot(tfs)
        return tfidfs, term_freqs

    def fit_batch(self, docs: List[str], doc_ids: List[Any]) -> None:
        """Fit batch of documents while fitting.

        Args:
            docs: a list of input documents
            doc_ids: a list of document ids corresponding to input documents

        Returns:
            None

        """

        for batch_rows, batch_data, batch_cols in self.get_counts(docs, doc_ids):
            self.rows.extend(batch_rows)
            self.cols.extend(batch_cols)
            self.data.extend(batch_data)

    def save(self) -> None:
        """Save tfidf matrix into .npz format.

        Returns:
            None

        """
        logger.info("Saving tfidf matrix to {}".format(self.save_path))
        count_matrix = self.get_count_matrix(self.rows, self.cols, self.data,
                                             size=len(self.doc_index))
        tfidf_matrix, term_freqs = self.get_tfidf_matrix(count_matrix)
        self.term_freqs = term_freqs

        opts = {'hash_size': self.hash_size,
                'ngram_range': self.tokenizer.ngram_range,
                'doc_index': self.doc_index,
                'term_freqs': self.term_freqs}

        data = {
            'data': tfidf_matrix.data,
            'indices': tfidf_matrix.indices,
            'indptr': tfidf_matrix.indptr,
            'shape': tfidf_matrix.shape,
            'opts': opts
        }
        np.savez(self.save_path, **data)

        # release memory
        self.reset()

    def reset(self) -> None:
        """Clear self.rows, self.cols and self.data

        Returns:
            None

        """
        self.rows.clear()
        self.cols.clear()
        self.data.clear()

    def load(self) -> Tuple[Sparse, Dict]:
        """Load a tfidf matrix as csr_matrix.

        Returns:
            a tuple of tfidf matrix and csr data.

        Todo:
            * implement loading from URL

        """
        logger.info("Loading tfidf matrix from {}".format(self.load_path))
        loader = np.load(self.load_path)
        matrix = sp.sparse.csr_matrix((loader['data'], loader['indices'],
                                       loader['indptr']), shape=loader['shape'])
        return matrix, loader['opts'].item(0)
