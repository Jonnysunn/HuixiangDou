# Copyright (c) OpenMMLab. All rights reserved.
"""extract feature and search with user query."""
import argparse
import json
import os
import re
import shutil
from pathlib import Path

import numpy as np
from BCEmbedding.tools.langchain import BCERerank
from file_operation import FileOperation
from langchain.embeddings import HuggingFaceEmbeddings
from langchain.retrievers import ContextualCompressionRetriever
from langchain.text_splitter import (MarkdownHeaderTextSplitter,
                                     MarkdownTextSplitter,
                                     RecursiveCharacterTextSplitter)
from langchain.vectorstores.faiss import FAISS as Vectorstore
from langchain_community.vectorstores.utils import DistanceStrategy
from langchain_core.documents import Document
from loguru import logger
from sklearn.metrics import precision_recall_curve
from torch.cuda import empty_cache


class Retriever:
    """Tokenize and extract features from the project's documents, for use in
    the reject pipeline and response pipeline."""

    def __init__(self, embeddings, reranker, work_dir: str,
                 reject_throttle: float) -> None:
        """Init with model device type and config."""
        self.reject_throttle = reject_throttle
        self.rejecter = Vectorstore.load_local(os.path.join(
            work_dir, 'db_reject'),
                                               embeddings=embeddings)
        self.retriever = Vectorstore.load_local(
            os.path.join(work_dir, 'db_response'),
            embeddings=embeddings,
            distance_strategy=DistanceStrategy.MAX_INNER_PRODUCT).as_retriever(
                search_type='similarity',
                search_kwargs={
                    'score_threshold': 0.2,
                    'k': 30
                })
        self.compression_retriever = ContextualCompressionRetriever(
            base_compressor=reranker, base_retriever=self.retriever)

    def cos_similarity(self, v1: list, v2: list):
        """Compute cos distance."""
        num = float(np.dot(v1, v2))
        denom = np.linalg.norm(v1) * np.linalg.norm(v2)
        return 0.5 + 0.5 * (num / denom) if denom != 0 else 0

    def distance(self, text1: str, text2: str):
        """Compute feature distance."""
        feature1 = self.embeddings.embed_query(text1)
        feature2 = self.embeddings.embed_query(text2)
        return self.cos_similarity(feature1, feature2)

    def is_reject(self, question, k=20, disable_throttle=False):
        """If no search results below the threshold can be found from the
        database, reject this query."""
        if disable_throttle:
            # for searching throttle during update sample
            docs_with_score = self.rejecter.similarity_search_with_relevance_scores(
                question, k=1)
            if len(docs_with_score) < 1:
                return True, docs_with_score
            return False, docs_with_score
        else:
            # for retrieve result
            # if no chunk passed the throttle, give the max
            docs_with_score = self.rejecter.similarity_search_with_relevance_scores(
                question, k=k)
            ret = []
            max_score = -1
            top1 = None
            for (doc, score) in docs_with_score:
                if score >= self.reject_throttle:
                    ret.append(doc)
                if score > max_score:
                    max_score = score
                    top1 = (doc, score)
            reject = False if len(ret) > 0 else True
            return reject, [top1]

    def query(self, question: str, context_max_length: int = 16000):
        """Processes a query and returns the best match from the vector store
        database. If the question is rejected, returns None.

        Args:
            question (str): The question asked by the user.

        Returns:
            str: The best matching chunk, or None.
            str: The best matching text, or None
        """
        if question is None or len(question) < 1:
            return None, None, []

        reject, docs = self.is_reject(question=question)
        assert (len(docs) > 0)
        if reject:
            return None, None, [
                os.path.basename(docs[0][0].metadata['source'])
            ]

        docs = self.compression_retriever.get_relevant_documents(question)
        chunks = []
        context = ''
        references = []

        # add file text to context, until exceed `context_max_length`
        import pdb
        pdb.set_trace()

        file_opr = FileOperation()
        for idx, doc in enumerate(docs):
            chunk = doc.page_content
            chunks.append(chunk)

            source = doc.metadata['source']
            file_text = file_opr.read_file(source)
            if len(file_text) + len(context) > context_max_length:
                references.append(source)
                # add and break
                add_len = context_max_length - len(context)
                if add_len <= 0:
                    break
                chunk_index = file_text.find(chunk)
                if chunk_index == -1:
                    # chunk not in file_text
                    context += chunk
                    context += '\n'
                    context += file_text[0:add_len - len(chunk) - 1]
                else:
                    start_index = max(0, chunk_index - (add_len - len(chunk)))
                    context += file_text[start_index:start_index + add_len]
                break

            if source not in references:
                context += file_text
                context += '\n'
                references.append(source)

        assert (len(context) <= context_max_length)
        logger.debug('query:{} top1 file:{}'.format(question, references[0]))
        return '\n'.join(chunks), context, [
            os.path.basename(r) for r in references
        ]
