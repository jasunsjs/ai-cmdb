"""LLM-backed implementations of the AI steps used by ingestion and /ask."""

from cmdb.ai.classifier import LLMValueClassifier
from cmdb.ai.client import LLMClient
from cmdb.ai.mapper import LLMMappingGenerator
from cmdb.ai.matcher import LLMEntityJudge
from cmdb.ai.sql import LLMAnswerWriter, LLMSqlGenerator
from cmdb.ask.service import AskAI
from cmdb.ingest.pipeline import IngestAI


def build_ingest_ai(llm: LLMClient | None) -> IngestAI | None:
    if llm is None:
        return None
    return IngestAI(
        mapper=LLMMappingGenerator(llm),
        classifier=LLMValueClassifier(llm),
        judge=LLMEntityJudge(llm),
    )


def build_ask_ai(llm: LLMClient | None) -> AskAI | None:
    if llm is None:
        return None
    return AskAI(sql_generator=LLMSqlGenerator(llm), answer_writer=LLMAnswerWriter(llm))
